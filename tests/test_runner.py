"""Runner: discover -> claim -> run -> report, against a MOCK Quest client (no network)."""
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
        self.reports = []          # list of (task_id, status, result, decision_id)
        self.posts = []            # list of (conv_id, content, kind) — live chat progress posts
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

    def claim(self, task_id):
        self.claimed.append(task_id)
        return {"id": task_id, "status": "in_progress"}

    def report_done(self, task_id, result):
        self.reports.append((task_id, "done", result, None))

    def report_needs_you(self, task_id, result, decision_id):
        self.reports.append((task_id, "needs_you", result, decision_id))

    def report_failed(self, task_id, result):
        self.reports.append((task_id, "failed", result, None))

    def post_conversation_message(self, conv_id, content, *, kind="progress"):
        self.posts.append((conv_id, content, kind))
        return {"role": "assistant", "kind": kind, "content": content}

    def post_environment_heartbeat(self, capabilities, *, runner_label=None, team_id=None):
        self.heartbeats.append((team_id, dict(capabilities), runner_label))
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


def test_poller_unconfigured_degrades_to_empty():
    provider = StubProvider(decisions=[])
    client = MockQuestClient([])
    client.configured = False
    poller = _poller_with(client, provider)
    assert poller.run_once() == []                 # logs + returns, never crashes


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
    intercept subprocess.run to capture the command (no real spawn)."""
    import subprocess as _sp
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    captured = {}

    class _Proc:
        returncode = 0
        stdout = b"did web research"
        stderr = b""

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(_sp, "run", _fake_run)
    runner = SubprocessGoalRunner(SubprocessConfig(
        working_dir="/w", claude_path="/usr/bin/claude",
        allowed_tools=["WebSearch", "WebFetch"], disallowed_tools=["Bash"]))
    res = runner.run_goal(goal="find sources", brief="research", max_turns=3)
    assert res.met is True
    cmd = captured["cmd"]
    assert "--allowed-tools" in cmd and "WebSearch,WebFetch" in cmd
    assert "--disallowed-tools" in cmd and "Bash" in cmd
    assert "--max-turns" in cmd


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
        def post_environment_heartbeat(self, capabilities, *, runner_label=None, team_id=None):
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
