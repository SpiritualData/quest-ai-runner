"""Runner: discover -> claim -> run -> report, against a MOCK Quest client (no network)."""
import logging
from datetime import datetime, timedelta, timezone

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.runner.poller import Poller, _task_signature

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class MockQuestClient:
    """An in-memory stand-in for QuestClient. Records every call; no HTTP.

    ``discover_due`` honors each task's ``due_at`` against ``now`` so scheduling can be tested:
    a future-due task is simply not returned (not claimable) until its due time. Tasks with no
    ``due_at`` count as due now (the poll-mode 'run now' = due now reduction). It also honors
    ``team_id`` the way the backend does: a non-empty filter narrows to that team's tasks (matched
    on each task's ``team_id``), while an empty/None filter is owner-scoped and returns all.
    """

    def __init__(self, due_tasks):
        self._due = list(due_tasks)
        self.configured = True
        self.claimed = []
        self.claim_handlers = []   # list of (task_id, handler) — what each claim stamped
        self.reports = []          # list of (task_id, status, result, decision_id)
        self.posts = []            # list of (conv_id, content, kind) — live chat progress posts
        self.post_task_ids = []    # parallel to ``posts``: the task_id (or None) each post carried
        self.progress = []         # list of (task_id, kind, text, output) — live task-detail stream
        self.heartbeats = []       # list of (team_id, capabilities, runner_label)
        self.discover_team_ids = []  # records the team_id each discovery scoped to

    def discover_due(self, *, now=None, status="queued", team_id=None):
        now = now or datetime.now(timezone.utc)
        self.discover_team_ids.append(team_id)
        out = []
        for t in self._due:
            due_at = t.get("due_at")
            if due_at and _parse_iso(due_at) > now:
                continue
            if team_id and t.get("team_id") != team_id:
                continue
            out.append(t)
        return out

    def claim(self, task_id, handler=None):
        self.claimed.append(task_id)
        self.claim_handlers.append((task_id, handler))
        return {"id": task_id, "status": "in_progress", "handler": handler}

    def report_progress(self, task_id, kind, *, text=None, output=None, data=None):
        self.progress.append((task_id, kind, text, output))
        return {"ok": True}

    def report_done(self, task_id, result):
        self.reports.append((task_id, "done", result, None))

    def report_needs_you(self, task_id, result, decision_id):
        self.reports.append((task_id, "needs_you", result, decision_id))

    def report_failed(self, task_id, result):
        self.reports.append((task_id, "failed", result, None))

    def post_conversation_message(self, conv_id, content, *, kind="progress", task_id=None):
        self.posts.append((conv_id, content, kind))
        self.post_task_ids.append(task_id)
        return {"role": "assistant", "kind": kind, "content": content}

    def post_environment_heartbeat(self, capabilities, *, runner_label=None, env_id=None, team_id=None):
        self.heartbeats.append((team_id, dict(capabilities), runner_label))
        self.last_env_id = env_id
        return {"team_id": team_id, "enabled": True, "reported_capabilities": dict(capabilities)}

    def whoami(self):
        return {"executor": "mock"}


def _brain(provider, **kw):
    return Orchestrator(retrieval=StubRetrieval({"README.md": "fact: yes"}),
                        provider=provider, registry=ModelRegistry(provider), **kw)


# --- executor unit tests --------------------------------------------------------

def test_executor_happy_path_reports_done():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t1", "text": "say hi"})
    assert out.status == "done"
    assert client.reports[0][0] == "t1"
    assert client.reports[0][1] == "done"


def test_executor_confirm_reports_needs_you_with_decision():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "approve purchase?", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_xyz")
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, escalation=sink))
    out = ex.execute({"id": "t2", "text": "buy thing", "quest_id": "q9"})
    assert out.status == "needs_you"
    assert out.decision_id == "dec_xyz"
    assert client.reports[0] == ("t2", "needs_you", "approve purchase?", "dec_xyz")


def test_executor_deep_met_reports_done():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "do X", "deep_brief": "x", "rationale": "work"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, deep_runner=StubDeepRunner(met=True, output="X done")))
    out = ex.execute({"id": "t3", "text": "do X"})
    assert out.status == "done"
    assert "X done" in client.reports[0][2]


def test_executor_deep_not_met_reports_failed():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "do X", "deep_brief": "x", "rationale": "work"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider,
                      deep_runner=StubDeepRunner(met=False, error="hit turn limit")))
    out = ex.execute({"id": "t4", "text": "do X"})
    assert out.status == "failed"
    assert "turn limit" in client.reports[0][2]


def test_executor_never_raises_on_brain_error():
    class BoomProvider(StubProvider):
        def plan(self, *a, **k):
            raise RuntimeError("model exploded")

    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(BoomProvider(decisions=[])))
    # The brain swallows planner errors -> grounded answer, so this still reports done.
    out = ex.execute({"id": "t5", "text": "anything"})
    assert out.status in ("done", "failed")
    assert client.reports  # something was reported


# --- live-progress-into-chat tests (conv_id linkage) ----------------------------

def test_executor_posts_live_progress_into_conversation():
    """A task carrying ``conv_id`` streams started → milestones → done INTO that chat.

    This is the core "chat won't go silent" proof at the executor level: a started message when
    the task is picked up, a progress post for each real deep milestone (the MilestoneSink only
    surfaces real milestones, never planning chatter), and a closing done message with the result.
    """
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "draft plan", "deep_brief": "x", "rationale": "work"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, deep_runner=StubDeepRunner(met=True, output="PLAN")))
    out = ex.execute({"id": "t6", "text": "do the overnight research", "conv_id": "qaconv_abc"})

    assert out.status == "done"
    kinds = [k for (_c, _t, k) in client.posts]
    convs = {c for (c, _t, _k) in client.posts}
    # All posts went to the originating conversation.
    assert convs == {"qaconv_abc"}
    # The lifecycle the chat sees: started first, a real milestone, done last.
    assert kinds[0] == "started"
    assert kinds[-1] == "done"
    assert "progress" in kinds            # the deep milestone surfaced
    # The done post carries the result text.
    assert any(t == "PLAN" and k == "done" for (_c, t, k) in client.posts)


def test_executor_progress_messages_accumulate_in_strict_order():
    """Flow 4 (deferred live-progress as MULTIPLE messages): the runner's posts arrive as a
    started → progress(…) → done SEQUENCE, in order, each a distinct append (no overwrite).

    A deep run that surfaces several milestones must produce: one ``started`` first, one
    ``progress`` per real milestone in the order they completed, and exactly one ``done`` last —
    so the chat shows the task working through to its result, not a single replaced message."""
    provider = StubProvider(decisions=[
        {"action": "deep",
         "deep_subtasks": [
             {"goal": "survey programs", "brief": "a"},
             {"goal": "compare options", "brief": "b"},
         ],
         "rationale": "multi-step work"},
    ])
    # A deep runner whose every subtask is met surfaces one milestone per subtask.
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, deep_runner=StubDeepRunner(met=True, output="RESULT")))
    out = ex.execute({"id": "tseq", "text": "overnight research", "conv_id": "qaconv_seq"})

    assert out.status == "done"
    posts = [(t, k) for (_c, t, k) in client.posts]
    kinds = [k for (_t, k) in posts]
    # started leads, done closes, and progress milestones sit strictly between them.
    assert kinds[0] == "started"
    assert kinds[-1] == "done"
    assert kinds.count("started") == 1
    assert kinds.count("done") == 1
    assert kinds.count("progress") >= 1
    # Every progress milestone is between the start and the final done (ordering, no overwrite).
    first_progress = kinds.index("progress")
    assert 0 < first_progress < len(kinds) - 1
    # Each post is a DISTINCT entry (no in-place replacement): count == sequence length.
    assert len(client.posts) == len(kinds)


def test_executor_no_conv_id_posts_nothing():
    """Without a ``conv_id`` (e.g. a reflection-delegated task) no chat posts are made — the
    behavior is exactly as before; only the task PATCH reports the result."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t7", "text": "say hi"})
    assert out.status == "done"
    assert client.posts == []


def test_executor_confirm_posts_decision_into_conversation():
    """A confirm/decision pause is also surfaced into the chat (kind='decision'), so the user
    sees WHY it paused rather than the chat going silent."""
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "approve purchase?", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_1")
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, escalation=sink))
    out = ex.execute({"id": "t8", "text": "buy thing", "conv_id": "qaconv_xyz"})
    assert out.status == "needs_you"
    assert ("qaconv_xyz", "approve purchase?", "decision") in client.posts


def test_executor_chat_post_failure_never_breaks_the_task():
    """If posting to the chat raises (network/conversation gone), the task still reports done."""
    class BoomPostClient(MockQuestClient):
        def post_conversation_message(self, conv_id, content, *, kind="progress"):
            raise RuntimeError("conversation post failed")

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = BoomPostClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t9", "text": "say hi", "conv_id": "qaconv_boom"})
    assert out.status == "done"
    assert client.reports and client.reports[0][1] == "done"


def test_executor_safe_logs_report_failure_at_error_with_traceback(caplog):
    """_safe() must log a swallowed report failure at ERROR with a traceback (exc_info), via the
    module logger, not print() to stdout — so it shows up in normal log capture/aggregation."""
    class BoomReportClient(MockQuestClient):
        def report_done(self, task_id, result):
            raise RuntimeError("report_done failed")

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = BoomReportClient([])
    ex = TaskExecutor(client, _brain(provider))
    with caplog.at_level(logging.ERROR, logger="quest-ai-runner.executor"):
        out = ex.execute({"id": "t9b", "text": "say hi"})
    assert out.status == "done"  # the report failure never breaks the task's own outcome
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("report failed" in r.message for r in error_records)
    assert any(r.exc_info is not None for r in error_records)  # traceback captured


def test_executor_posts_progress_for_team_conv_id_verbatim():
    """A team-chat-delegated task carries a ``team:{team_id}`` conv id (not a ``qaconv_`` id).

    The runner must treat it like any other conversation id: post started → … → done into it,
    passing the id VERBATIM (no prefix check, no rewrite). This is the executor-level proof that
    the colon-prefixed team scope flows through to ``post_conversation_message`` unchanged."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "t10", "text": "say hi to the team", "conv_id": "team:abc123"})
    assert out.status == "done"
    convs = {c for (c, _t, _k) in client.posts}
    # Every post went to the team conversation id, verbatim (colon intact, no rewrite).
    assert convs == {"team:abc123"}
    kinds = [k for (_c, _t, k) in client.posts]
    assert kinds[0] == "started"
    assert kinds[-1] == "done"


def test_quest_client_progress_url_keeps_colon_for_team_conv_id(monkeypatch):
    """QuestClient.post_conversation_message must POST to
    ``/api/quest-ai/conversations/{conv_id}/progress`` with the conv id placed in the path
    VERBATIM — including a literal ``:`` for a ``team:{team_id}`` id (the Quest route accepts a
    bare colon in a path segment). Prove the built URL keeps the colon and the body is unchanged,
    by mocking the HTTP layer (no network)."""
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"role": "assistant"}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    out = client.post_conversation_message("team:abc123", "Started working on this", kind="started")

    assert out == {"role": "assistant"}
    # The colon survives in the path segment, exactly once, before /progress.
    assert captured["url"] == "http://quest.example/api/quest-ai/conversations/team:abc123/progress"
    assert "%3A" not in captured["url"]   # the colon is NOT percent-encoded
    assert captured["method"] == "POST"
    # Body shape is unchanged: {content, kind}.
    assert json.loads(captured["body"]) == {"content": "Started working on this", "kind": "started"}


def test_quest_client_progress_url_same_shape_for_qaconv_id(monkeypatch):
    """Regression guard: the SAME method builds the SAME URL shape for a ``qaconv_`` id, so the
    team-id path isn't a special case — both ids are simply interpolated verbatim."""
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    client.post_conversation_message("qaconv_xyz", "progress", kind="progress")
    assert captured["url"] == "http://quest.example/api/quest-ai/conversations/qaconv_xyz/progress"


# --- poller integration tests ---------------------------------------------------

def _poller_with(client, provider, *, team_id="team1", **kw):
    from quest_ai_runner.config import RunnerConfig
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id=team_id,
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider, **kw,
    )
    p = Poller(cfg, state_path=None, client=client)
    return p


def test_poller_discover_claim_run_report_happy_path():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([{"id": "task-1", "text": "summarize the README", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider)
    handled = poller.run_once()
    assert handled == ["task-1"]
    assert client.claimed == ["task-1"]            # it claimed before running
    assert client.reports[0][:2] == ("task-1", "done")


def test_poller_needs_you_decision_path():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "approve?", "rationale": "human-only"},
    ])
    client = MockQuestClient([{"id": "task-2", "text": "buy something", "status": "queued", "team_id": "team1"}])
    # No explicit escalation -> the poller defaults to a QuestDecisionSink over the client.
    # Our mock client doesn't implement create_decision, so wire an explicit stub sink instead.
    poller = _poller_with(client, provider, escalation=StubEscalation(decision_id="dec_1"))
    handled = poller.run_once()
    assert handled == ["task-2"]
    assert client.reports[0] == ("task-2", "needs_you", "approve?", "dec_1")


def test_poller_dedup_skips_already_handled():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"},
                                       {"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([{"id": "task-3", "text": "x", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider)
    assert poller.run_once() == ["task-3"]
    # Same task still 'due' on the next scan, but the signature store dedups it.
    assert poller.run_once() == []
    assert client.claimed == ["task-3"]            # claimed exactly once


def test_poller_skips_execution_when_claim_fails_and_reoffers_later():
    """claim() returning None (already claimed elsewhere / transient API error) must not execute
    the task, and must NOT permanently mark it handled — so it is re-offered on a later scan."""
    class _ClaimFailsClient(MockQuestClient):
        def claim(self, task_id, handler=None):
            self.claimed.append(task_id)
            return None

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = _ClaimFailsClient([{"id": "task-4", "text": "x", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider)

    handled = poller.run_once()
    assert handled == []                    # not executed
    assert client.reports == []             # no report was filed
    assert provider.plan_calls == 0         # the brain never ran

    # The signature was NOT marked, so the same due task is re-offered on the next scan.
    assert poller.run_once() == []          # still fails to claim, still not executed
    assert client.claimed == ["task-4", "task-4"]   # attempted to claim on each scan


def test_poller_unconfigured_degrades_to_empty():
    provider = StubProvider(decisions=[])
    client = MockQuestClient([])
    client.configured = False
    poller = _poller_with(client, provider)
    assert poller.run_once() == []                 # logs + returns, never crashes


def test_poller_handles_futures_in_completion_order_not_submission_order():
    """run_once() must drive per-task handling via as_completed(), so a FAST task is handled/
    logged as soon as it finishes rather than waiting behind a SLOW task submitted earlier."""
    import time

    provider = StubProvider(decisions=[])
    client = MockQuestClient([
        {"id": "slow-task", "text": "x", "status": "queued", "team_id": "team1"},
        {"id": "fast-task", "text": "x", "status": "queued", "team_id": "team1"},
    ])
    poller = _poller_with(client, provider)

    completion_order = []
    real_handle_one = poller._handle_one

    def _fake_handle_one(task):
        task_id = task.get("id")
        if task_id == "slow-task":
            time.sleep(0.2)
        else:
            time.sleep(0.01)
        completion_order.append(task_id)
        return task_id

    poller._handle_one = _fake_handle_one
    handled = poller.run_once()

    # Both ran, but the FAST one (submitted second) completed and was recorded first.
    assert completion_order == ["fast-task", "slow-task"]
    assert handled == ["fast-task", "slow-task"]  # run_once's return order follows completion too


def test_task_signature_is_stable_and_status_sensitive():
    t = {"id": "a", "status": "queued"}
    assert _task_signature(t) == _task_signature(dict(t))
    assert _task_signature(t) != _task_signature({"id": "a", "status": "in_progress"})


# --- scheduling: a future due-time is simply not-yet-claimed until due ------------

def test_scheduled_future_task_is_not_claimed_until_due():
    """Quest scheduling = the due time; the runner is timing-agnostic ('what's due?').
    A task stamped due in the future is not discovered/claimed until then."""
    iso = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"},
                                       {"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([
        {"id": "now-task", "text": "do now", "status": "queued", "team_id": "team1"},  # no due_at => due now
        {"id": "later-task", "text": "do later", "status": "queued", "team_id": "team1", "due_at": iso(future)},
    ])
    poller = _poller_with(client, provider)

    handled = poller.run_once()
    assert handled == ["now-task"]                  # only the due-now task ran
    assert "later-task" not in client.claimed       # the future task was never claimed


def test_scheduled_task_runs_once_its_due_time_passes():
    iso = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([
        {"id": "due-task", "text": "do it", "status": "queued", "team_id": "team1", "due_at": iso(past)},
    ])
    poller = _poller_with(client, provider)
    assert poller.run_once() == ["due-task"]         # past-due => claimed and run
    assert client.claimed == ["due-task"]


# --- environment heartbeat: the runner advertises liveness + capabilities each scan ----------

def test_subprocess_config_web_enabled_derivation():
    """web_enabled() honestly tracks the worker's tool gating: default = web on (Claude Code ships
    WebSearch/WebFetch), pinning without web = off, allowing a web tool = on, disallowing = off."""
    from quest_ai_runner.core.goal_runner import SubprocessConfig

    assert SubprocessConfig(working_dir="/w").web_enabled() is True
    assert SubprocessConfig(working_dir="/w", allowed_tools=["Read", "Bash"]).web_enabled() is False
    assert SubprocessConfig(working_dir="/w", allowed_tools=["WebSearch"]).web_enabled() is True
    assert SubprocessConfig(working_dir="/w", disallowed_tools=["WebFetch", "WebSearch"]).web_enabled() is False


def test_subprocess_runner_passes_tool_flags_and_runs_web_goal(monkeypatch):
    """When tools are pinned the runner passes --allowed-tools/--disallowed-tools to Claude Code, so
    what the env ADVERTISES (web_enabled) matches what the worker is actually allowed to do. We
    intercept subprocess.Popen to capture the command (no real spawn)."""
    import subprocess as _sp
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    captured = {}

    class _MockPopen:
        returncode = 0
        stdin = None
        def communicate(self, input=None, timeout=None):
            captured["cmd_from_popen"] = captured.get("cmd")
            return (b"did web research", b"")

    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _MockPopen()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    runner = SubprocessGoalRunner(SubprocessConfig(
        working_dir="/w", claude_path="/usr/bin/claude",
        allowed_tools=["WebSearch", "WebFetch"], disallowed_tools=["Bash"]))
    res = runner.run_goal(goal="find sources", brief="research", max_turns=3)
    assert res.met is True
    cmd = captured["cmd"]
    # -p/--print is MANDATORY: without it Claude Code runs interactively and exits doing nothing.
    assert "-p" in cmd
    assert "--allowed-tools" in cmd and "WebSearch,WebFetch" in cmd
    assert "--disallowed-tools" in cmd and "Bash" in cmd
    assert "--max-turns" in cmd


def test_subprocess_runner_drops_non_claude_model(monkeypatch):
    """The Claude Code worker only runs Claude models. A non-Claude tier model (e.g. a Gemini id
    from the consumer's config) must NOT be passed as --model, or Claude Code errors and does
    nothing. A Claude model is passed through."""
    import subprocess as _sp
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    captured = {}

    class _MockPopen:
        returncode = 0
        stdin = None
        def communicate(self, input=None, timeout=None):
            return (b"did it", b"")

    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _MockPopen()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))

    runner.run_goal(goal="g", brief="b", model="gemini-3.5-flash", max_turns=2)
    assert "--model" not in captured["cmd"], "non-Claude model must not reach Claude Code"

    captured.clear()
    runner.run_goal(goal="g", brief="b", model="claude-opus-4-8", max_turns=2)
    assert "--model" in captured["cmd"] and "claude-opus-4-8" in captured["cmd"]


def test_subprocess_runner_treats_empty_output_as_not_met(monkeypatch):
    """Exit 0 with NO output is a silent no-op (the worker never ran the goal), not a success.
    The runner must report met=False with a clear message rather than a hollow 'Completed'."""
    import subprocess as _sp
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    class _MockPopen:
        returncode = 0
        stdin = None
        def communicate(self, input=None, timeout=None):
            return (b"   \n", b"")

    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: _MockPopen())
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))
    res = runner.run_goal(goal="fix the date bug", brief="do it", max_turns=3)
    assert res.met is False
    assert "no output" in (res.error or "").lower()


# --- the escalation-marker contract: a deep worker that raised a human decision --------------

def test_extract_escalation_id():
    from quest_ai_runner.core.goal_runner import extract_escalation_id

    assert extract_escalation_id("no marker here") is None
    assert extract_escalation_id("") is None
    assert extract_escalation_id("did work\nQAR-ESCALATED: dec_42\n") == "dec_42"
    # Whitespace-tolerant, and the LAST marker wins (a worker may quote an earlier one).
    assert extract_escalation_id("  QAR-ESCALATED:   dec_a  \nmore\nQAR-ESCALATED: dec_b") == "dec_b"
    # A bare marker with no id is not an escalation.
    assert extract_escalation_id("QAR-ESCALATED:\n") is None


def test_subprocess_runner_parses_escalation_marker(monkeypatch):
    """A spawned worker that raised a human decision mid-run prints ``QAR-ESCALATED: <id>``; the
    runner returns met=False + decision_id so the executor reports needs_you with the decision
    linked — regardless of the worker's exit code (escalated-and-exited-clean is still paused)."""
    import subprocess as _sp
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    class _MockPopen:
        returncode = 0
        stdin = None
        def communicate(self, input=None, timeout=None):
            return (b"Drafted the email; sending needs approval.\nQAR-ESCALATED: dec_99\n", b"")

    monkeypatch.setattr(_sp, "Popen", lambda cmd, **kw: _MockPopen())
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))
    res = runner.run_goal(goal="send the email", brief="draft + send", max_turns=3)
    assert res.met is False
    assert res.decision_id == "dec_99"
    assert "Drafted the email" in res.output
    assert res.error is None


def test_goal_runner_normalizes_decision_to_not_met():
    """GoalRunner never lets a runner report met=True alongside a decision_id — a paused-on-human
    run must reach the executor as not-met so it reports needs_you, not done."""
    from quest_ai_runner.core.goal_runner import GoalRunner

    gr = GoalRunner(StubDeepRunner(met=True, output="staged", decision_id="dec_7"))
    res = gr.run(goal="g", brief="b")
    assert res.met is False
    assert res.decision_id == "dec_7"


def test_executor_deep_decision_reports_needs_you():
    """A deep run that escalated (DeepResult.decision_id set) reports needs_you with that decision
    id, and the prepared output is what lands in the originating chat."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "do X", "deep_brief": "x", "rationale": "work"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider, deep_runner=StubDeepRunner(
        met=False, output="staged the order for approval", decision_id="dec_55")))
    out = ex.execute({"id": "t9", "text": "order the part", "conv_id": "qaconv_1"})
    assert out.status == "needs_you"
    assert out.decision_id == "dec_55"
    assert client.reports[0][1] == "needs_you"
    assert client.reports[0][3] == "dec_55"
    assert ("qaconv_1", "staged the order for approval", "decision") in client.posts


def test_poller_discovery_is_team_scoped_to_the_lanes_team():
    """The lane passes its configured team_id into discovery so two teams under the SAME owner are
    isolated: the team-A lane discovers ONLY team-A tasks, never team-B's. (Backend enforces the
    actual filter; here we prove the runner SCOPES the call and acts only on its team's tasks.)"""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([
        {"id": "task-A", "text": "A work", "status": "queued", "team_id": "team1"},
        {"id": "task-B", "text": "B work", "status": "queued", "team_id": "team_other"},
    ])
    # _poller_with configures team_id="team1" — the team-A lane.
    poller = _poller_with(client, provider)
    handled = poller.run_once()
    assert handled == ["task-A"]                       # only team-A's task ran
    assert "task-B" not in client.claimed              # team-B's task was never claimed
    assert client.discover_team_ids == ["team1"]       # discovery was scoped to the lane's team


def test_poller_teamless_lane_discovers_owner_scoped():
    """A lane with no team_id (the personal lane) passes an empty team filter, preserving the
    existing owner-scoped discovery contract — it still sees all the owner's tasks."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([
        {"id": "p1", "text": "personal task", "status": "queued"},  # no team_id
    ])
    poller = _poller_with(client, provider)
    poller.cfg.team_id = ""                             # personal/teamless lane
    handled = poller.run_once()
    assert handled == ["p1"]
    assert client.discover_team_ids == [""]            # owner-scoped (no team narrowing)


def test_poller_emits_heartbeat_each_scan_with_derived_capabilities():
    """Every poll cycle the runner heartbeats the backend with the capabilities derived from its
    wired adapters. Here: a deep_runner (=> code) and a corpus_root (=> corpus). The stub deep-runner
    exposes no SubprocessConfig tooling, so web is reported FALSE — derivation reads the actual
    runner's tool gating (web_enabled), never a hardcode, and a runner that can't prove web is web:false."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])  # no due tasks: heartbeat must still fire
    poller = _poller_with(
        client, provider,
        deep_runner=StubDeepRunner(met=True, output="x"), corpus_root="/some/corpus",
    )
    poller.cfg.runner_label = "test-runner"

    handled = poller.run_once()
    assert handled == []                       # nothing to run
    assert len(client.heartbeats) == 1         # but the env heartbeat still fired
    team_id, caps, label = client.heartbeats[0]
    assert team_id == "team1"
    assert label == "test-runner"
    assert caps == {"web": False, "corpus": True, "code": True}


def test_poller_heartbeat_reports_web_via_subprocess_runner_default():
    """The reference deep-runner spawns Claude Code, which ships WebSearch/WebFetch. With the
    DEFAULT SubprocessConfig (skip_permissions, no tool restrictions) the worker CAN browse, so the
    env honestly advertises web:true. Proves web is derived from the SubprocessConfig's real tool
    gating, not a hardcode."""
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    deep = SubprocessGoalRunner(SubprocessConfig(working_dir="/tmp/work"))
    poller = _poller_with(client, provider, deep_runner=deep, corpus_root="/some/corpus")
    poller.run_once()
    assert client.heartbeats
    _team, caps, _label = client.heartbeats[0]
    assert caps == {"web": True, "corpus": True, "code": True}


def test_poller_heartbeat_reports_web_false_when_subprocess_tools_pinned_without_web():
    """If a consumer PINS the subprocess to a tool set without the web tools (or disallows them),
    the env reports web:false honestly — the derivation tracks the actual gating both ways."""
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    deep = SubprocessGoalRunner(SubprocessConfig(
        working_dir="/tmp/work", allowed_tools=["Read", "Grep", "Bash"]))
    poller = _poller_with(client, provider, deep_runner=deep)
    poller.run_once()
    assert client.heartbeats
    _team, caps, _label = client.heartbeats[0]
    assert caps["web"] is False and caps["code"] is True


def test_poller_heartbeat_reports_web_false_via_files_adapter_no_deep_runner():
    """A real FilesAdapter satisfies corpus; with NO deep_runner there's nothing that can browse,
    so code AND web are False. Proves derivation reads the actual wiring, not a hardcoded claim."""
    import tempfile
    from quest_ai_runner.adapters import FilesAdapter

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    with tempfile.TemporaryDirectory() as d:
        from quest_ai_runner.config import RunnerConfig
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=FilesAdapter(d), model_provider=provider,
        )
        poller = Poller(cfg, state_path=None, client=client)
        poller.run_once()
    assert client.heartbeats
    _team, caps, _label = client.heartbeats[0]
    assert caps == {"web": False, "corpus": True, "code": False}


def test_poller_heartbeat_failure_never_breaks_task_execution():
    """If the heartbeat POST raises (endpoint down, network), the scan still discovers, claims,
    runs, and reports the due task. Best-effort, exactly like progress-posting."""
    class BoomHeartbeatClient(MockQuestClient):
        def post_environment_heartbeat(self, capabilities, *, runner_label=None, env_id=None, team_id=None):
            raise RuntimeError("heartbeat endpoint unavailable")

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = BoomHeartbeatClient([{"id": "task-h", "text": "do it", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider)
    handled = poller.run_once()
    assert handled == ["task-h"]                # the task still ran despite the heartbeat error
    assert client.reports[0][:2] == ("task-h", "done")


def test_poller_no_team_id_skips_heartbeat_cleanly():
    """With no team_id configured there's nothing to attach an env to — the heartbeat is skipped
    (not an error) and the poll proceeds normally."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    from quest_ai_runner.config import RunnerConfig
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == []
    assert client.heartbeats == []             # nothing heartbeated, no crash


# --- handler stamping + live task-detail progress stream ------------------------

def test_claim_includes_handler_in_patch_body(monkeypatch):
    """QuestClient.claim(handler=...) must PATCH /api/assistant-tasks/{id} with the handler in the
    body (alongside status), so the task records WHO ran it. Mock the HTTP layer (no network)."""
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"id": "t1", "status": "in_progress"}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    client.claim("t1", handler="joshua")

    assert captured["url"] == "http://quest.example/api/assistant-tasks/t1"
    assert captured["method"] == "PATCH"
    assert json.loads(captured["body"]) == {"status": "in_progress", "handler": "joshua"}


def test_claim_without_handler_omits_it(monkeypatch):
    """Backward compatibility: claim() with no handler sends only {status} — body unchanged."""
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b''

    def _fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    client.claim("t1")
    assert json.loads(captured["body"]) == {"status": "in_progress"}


def test_report_progress_posts_to_right_path_and_body(monkeypatch):
    """report_progress POSTs to /api/assistant-tasks/{id}/progress with only the non-None
    fields ({kind, text?, output?, data?}), mirroring post_conversation_message's body build."""
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    out = client.report_progress("t1", "exec", text="ran step 2", output="raw")

    assert out == {"ok": True}
    assert captured["url"] == "http://quest.example/api/assistant-tasks/t1/progress"
    assert captured["method"] == "POST"
    # Only the supplied non-None fields are sent; no data key when omitted.
    assert json.loads(captured["body"]) == {"kind": "exec", "text": "ran step 2", "output": "raw"}


def test_report_progress_swallows_http_errors(monkeypatch):
    """A progress-post failure must NEVER raise (it can't be allowed to fail the task): an HTTP
    error is logged and an empty dict returned."""
    import urllib.error
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    def _boom_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", _boom_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    # Must not raise, and returns {} on failure.
    assert client.report_progress("t1", "started", text="hi") == {}


def test_poller_stamps_handler_from_rep_skill_dir():
    """When a rep_sync_resolver maps a task to (user_id, skill_dir), the poller claims with the
    handler = basename(skill_dir) (the rep slug)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([{"id": "task-r", "text": "do it", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(
        client, provider,
        rep_sync_resolver=lambda task: ("user-123", "/somewhere/skills/subham"))
    handled = poller.run_once()
    assert handled == ["task-r"]
    assert client.claim_handlers == [("task-r", "subham")]


def test_poller_handler_falls_back_to_runner_label():
    """With no rep_sync_resolver, the handler falls back to the configured runner_label."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([{"id": "task-l", "text": "do it", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider, runner_label="sd-team-runner")
    poller.run_once()
    assert client.claim_handlers == [("task-l", "sd-team-runner")]


def test_poller_handler_none_when_no_resolver_no_label():
    """No resolver and no runner_label -> handler is None (claim body omits it; backward compat)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([{"id": "task-n", "text": "do it", "status": "queued", "team_id": "team1"}])
    poller = _poller_with(client, provider)
    poller.run_once()
    assert client.claim_handlers == [("task-n", None)]


def test_executor_emits_started_and_terminal_done_progress():
    """The executor posts a 'started' progress event when it picks the task up and a terminal
    'done' event mapped from a successful result — onto the task-detail stream (independent of any
    conv_id chat linkage)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tp", "text": "say hi"})
    assert out.status == "done"
    kinds = [k for (_tid, k, _t, _o) in client.progress]
    assert kinds[0] == "started"
    assert kinds[-1] == "done"
    # Every progress event was stamped onto THIS task.
    assert {tid for (tid, _k, _t, _o) in client.progress} == {"tp"}


def test_executor_emits_error_progress_on_failed_deep():
    """A deep run that isn't met maps to a terminal 'error' progress event."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "do X", "deep_brief": "x", "rationale": "work"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider,
                      deep_runner=StubDeepRunner(met=False, error="hit turn limit")))
    out = ex.execute({"id": "tpe", "text": "do X"})
    assert out.status == "failed"
    kinds = [k for (_tid, k, _t, _o) in client.progress]
    assert kinds[0] == "started"
    assert kinds[-1] == "error"


def test_executor_progress_swallowed_when_client_lacks_method():
    """An older client without report_progress is fine — the executor no-ops the stream and still
    reports the result (backward compatible)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])

    class NoProgressClient(MockQuestClient):
        report_progress = None     # not callable -> the helper must skip it

    client = NoProgressClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tnp", "text": "say hi"})
    assert out.status == "done"
    assert client.reports and client.reports[0][1] == "done"


# ---------------------------------------------------------------------------
# Per-task model hint (task["model"] -> model_hint -> provider).
# ---------------------------------------------------------------------------

class _ModelCapturingProvider(StubProvider):
    """StubProvider that records the model id passed to answer() and plan()."""
    def __init__(self, decisions):
        super().__init__(decisions)
        self.answer_models: list = []

    def answer(self, messages, *, model, system=None):
        self.answer_models.append(model)
        return super().answer(messages, model=model, system=system)


def test_executor_task_model_field_reaches_provider():
    """A task carrying ``model`` has that hint forwarded to the orchestrator and on to the
    provider: the model id supplied to answer() corresponds to the hint, not the planner's tier."""
    provider = _ModelCapturingProvider(decisions=[
        # Planner picks haiku; the task hint should override this.
        {"action": "answer", "model_tier": "haiku", "rationale": "ok"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tmh1", "text": "say hi", "model": "opus"})
    assert out.status == "done"
    # The provider must have been called with the "opus" resolution, not "haiku".
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_opus = registry.resolve_tier("opus")
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier (never the hint), before the real, hinted answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_opus]


def test_executor_task_without_model_field_uses_planner_tier():
    """A task without a ``model`` field leaves the existing planner-tier logic intact."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "rationale": "ok"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tmh2", "text": "say hi"})
    assert out.status == "done"
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_balanced = registry.resolve_tier("balanced")  # planner_tier default = balanced
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier, before the real answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_balanced]


def test_executor_task_model_none_is_same_as_absent():
    """Explicit ``model=None`` on a task is the same as omitting it entirely."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "ok"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tmh3", "text": "say hi", "model": None})
    assert out.status == "done"
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_sonnet = registry.resolve_tier("sonnet")
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier, before the real answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_sonnet]


# ---------------------------------------------------------------------------
# Mid-run cancellation (POST /api/assistant-tasks/{id}/undo sets status=cancelled +
# cancel_requested=true; the runner must notice it and stop cooperatively).
# ---------------------------------------------------------------------------

def test_executor_cancel_check_stops_mid_run_and_skips_terminal_report():
    """A cancel_check that flips True mid-run (after the first plan/read step) must stop the
    orchestrator cleanly with a cancelled outcome, and the executor must NOT PATCH a terminal
    status (report_done/report_needs_you/report_failed) or post a done/failed message into the
    chat -- the backend already owns the terminal state and a PATCH would just 409."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "reading"},
        {"action": "answer", "rationale": "should never run"},
    ])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))

    calls = {"n": 0}

    def cancel_after_first_step() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    ex._build_cancel_check = lambda task_id: cancel_after_first_step
    out = ex.execute({"id": "tcancel", "text": "say hi", "conv_id": "qaconv_cancel"})

    assert out.status == "cancelled"
    assert provider.plan_calls == 1              # stopped before the second (answer) step ran
    assert client.reports == []                  # no report_done/needs_you/failed PATCH
    # The unavoidable pre-run "started" message is the ONLY chat post -- no terminal message.
    kinds = [k for (_c, _t, k) in client.posts]
    assert kinds == ["started"]
    progress_kinds = [k for (_tid, k, _t, _o) in client.progress]
    assert progress_kinds[0] == "started"
    assert progress_kinds[-1] == "status"        # the quiet-cancelled progress note


def test_executor_cancel_check_none_when_client_lacks_is_task_cancelled():
    """The default (real) ``_build_cancel_check`` degrades to an always-False check when the
    client has no ``is_task_cancelled`` (older clients / the mock in this file lacks it too) --
    a normal run must be completely unaffected."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tnorm1", "text": "say hi"})
    assert out.status == "done"
    assert client.reports[0][:2] == ("tnorm1", "done")


def test_executor_normal_run_unaffected_when_never_cancelled():
    """A client that DOES implement ``is_task_cancelled`` but always reports False must leave a
    normal run byte-for-byte unaffected (done is still PATCHed and posted)."""
    class NeverCancelledClient(MockQuestClient):
        def is_task_cancelled(self, task_id):
            return False

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = NeverCancelledClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tnorm2", "text": "say hi", "conv_id": "qaconv_norm"})
    assert out.status == "done"
    assert client.reports[0][:2] == ("tnorm2", "done")
    assert ("qaconv_norm",) == tuple({c for (c, _t, _k) in client.posts})


def test_executor_final_check_maps_orchestrator_error_to_cancelled_when_task_was_cancelled():
    """If the orchestrator run raises (e.g. it was interrupted) but the task turns out to already
    be cancelled, the executor must report the quiet-cancelled outcome instead of failed -- a run
    that dies BECAUSE it was interrupted must not be reported as a genuine failure."""
    class BoomProvider(StubProvider):
        def plan(self, *a, **k):
            raise RuntimeError("interrupted mid-call")

    class CancelledAfterTheFactClient(MockQuestClient):
        def is_task_cancelled(self, task_id):
            return True

    client = CancelledAfterTheFactClient([])
    ex = TaskExecutor(client, _brain(BoomProvider(decisions=[])))
    out = ex.execute({"id": "tboom", "text": "do something"})
    assert out.status == "cancelled"
    assert client.reports == []                  # no report_failed PATCH


def test_executor_posts_include_task_id_for_conversation_correlation():
    """Every chat post the executor makes carries the task's own id, so the frontend can correlate
    a progress message back to the task it belongs to (e.g. to show/hide a stop control)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "tcorr", "text": "say hi", "conv_id": "qaconv_corr"})
    assert out.status == "done"
    assert client.post_task_ids and all(t == "tcorr" for t in client.post_task_ids)


def test_build_cancel_check_throttles_is_task_cancelled_calls():
    """The default THROTTLE interval (~15s) means repeated rapid checks must hit the client's
    ``is_task_cancelled`` at most once -- the rest reuse the last known answer."""
    calls = []

    class CountingClient(MockQuestClient):
        def is_task_cancelled(self, task_id):
            calls.append(task_id)
            return False

    client = CountingClient([])
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    check = ex._build_cancel_check("t1")
    for _ in range(20):
        assert check() is False
    assert len(calls) == 1


def test_build_cancel_check_rechecks_after_interval_elapses():
    """With ``interval=0`` every call is past the (zero-length) window, so every check re-hits the
    client -- proving the throttle is TIME-based, not a one-shot cache."""
    calls = []

    class CountingClient(MockQuestClient):
        def is_task_cancelled(self, task_id):
            calls.append(task_id)
            return False

    client = CountingClient([])
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    check = ex._build_cancel_check("t1", interval=0.0)
    check()
    check()
    check()
    assert len(calls) == 3


def test_build_cancel_check_no_task_id_is_always_false():
    """No task id (shouldn't happen in practice, but must degrade safely) -> always-False check,
    no client call at all."""
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(StubProvider(decisions=[])))
    check = ex._build_cancel_check("")
    assert check() is False


# --- QuestClient.is_task_cancelled ------------------------------------------------

def test_is_task_cancelled_true_for_status_cancelled():
    from quest_ai_runner.runner.quest_client import QuestClient

    class _FakeGetTaskClient(QuestClient):
        def get_task(self, task_id):
            return {"id": task_id, "status": "cancelled"}

    client = _FakeGetTaskClient("http://quest.example", "qsk_test")
    assert client.is_task_cancelled("t1") is True


def test_is_task_cancelled_true_for_cancel_requested_flag():
    from quest_ai_runner.runner.quest_client import QuestClient

    class _FakeGetTaskClient(QuestClient):
        def get_task(self, task_id):
            return {"id": task_id, "status": "in_progress", "cancel_requested": True}

    client = _FakeGetTaskClient("http://quest.example", "qsk_test")
    assert client.is_task_cancelled("t1") is True


def test_is_task_cancelled_false_when_neither_signal_set():
    from quest_ai_runner.runner.quest_client import QuestClient

    class _FakeGetTaskClient(QuestClient):
        def get_task(self, task_id):
            return {"id": task_id, "status": "in_progress", "cancel_requested": False}

    client = _FakeGetTaskClient("http://quest.example", "qsk_test")
    assert client.is_task_cancelled("t1") is False


def test_is_task_cancelled_false_on_api_error():
    """FAIL-OPEN by contract: a transient error must never be mistaken for a cancellation and kill
    a legitimate run."""
    from quest_ai_runner.runner.quest_client import QuestClient

    class _BoomGetTaskClient(QuestClient):
        def get_task(self, task_id):
            raise RuntimeError("network boom")

    client = _BoomGetTaskClient("http://quest.example", "qsk_test")
    assert client.is_task_cancelled("t1") is False


def test_is_task_cancelled_false_when_task_not_found():
    """``get_task`` already degrades to {} on a 404/API error (see its own docstring); an empty
    task has neither signal, so this must be False, not raise."""
    from quest_ai_runner.runner.quest_client import QuestClient

    class _MissingTaskClient(QuestClient):
        def get_task(self, task_id):
            return {}

    client = _MissingTaskClient("http://quest.example", "qsk_test")
    assert client.is_task_cancelled("missing") is False


# --- QuestClient.post_conversation_message(task_id=...) ---------------------------

def test_post_conversation_message_includes_task_id_when_given(monkeypatch):
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"role": "assistant"}'

    def _fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    client.post_conversation_message("qaconv_1", "hello", kind="progress", task_id="task-99")
    assert json.loads(captured["body"]) == {
        "content": "hello", "kind": "progress", "task_id": "task-99",
    }


def test_post_conversation_message_omits_task_id_when_absent(monkeypatch):
    """Backward compatibility: no ``task_id`` -> the body is exactly {content, kind}, unchanged."""
    import json
    import urllib.request
    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"role": "assistant"}'

    def _fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test")
    client.post_conversation_message("qaconv_1", "hello", kind="progress")
    assert json.loads(captured["body"]) == {"content": "hello", "kind": "progress"}
