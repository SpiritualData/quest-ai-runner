"""Opt-in reps run tasks AS their Quest persona by default; rep_sync_direction gates push-back.

All offline: a fake Quest client (task + AI-profile surface) + a capturing deep runner. No
network, no API key. These prove the DEFAULT-correct behaviour: turn reps on (a resolver) and the
runner pulls the rep, injects its persona into the deep run, and (only when asked) pushes back.
"""
import tempfile
from pathlib import Path

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.runner.poller import Poller

from .conftest import StubProvider, StubRetrieval
from .test_rep_sync import MockProfileClient
from .test_runner import MockQuestClient


class CapturingDeepRunner:
    """A DeepRunner whose ``run_goal`` ACCEPTS and records ``context_preamble`` (opt-in seam).

    This is exactly the shape that lets the orchestrator forward a per-task rep preamble: a runner
    that accepts ``context_preamble`` opts in; one that does not (the conftest StubDeepRunner) is
    left untouched. We capture every call so a test can assert WHAT preamble reached the deep run.
    """

    def __init__(self, met: bool = True, output: str = "deep done"):
        self._met = met
        self._output = output
        self.calls = []  # list of dicts: {goal, brief, model, max_turns, context_preamble}

    def run_goal(self, *, goal, brief, model=None, max_turns=None,
                 context_preamble=None) -> DeepResult:
        self.calls.append({
            "goal": goal, "brief": brief, "model": model, "max_turns": max_turns,
            "context_preamble": context_preamble,
        })
        return DeepResult(met=self._met, output=self._output)


def _brain(provider, deep_runner):
    return Orchestrator(retrieval=StubRetrieval({"README.md": "fact"}),
                        provider=provider, registry=ModelRegistry(provider),
                        deep_runner=deep_runner)


def _profile_aware_client():
    """A MockQuestClient (task surface) wired with a MockProfileClient's AI-profile surface."""
    profile_client = MockProfileClient()
    client = MockQuestClient([
        {"id": "rep-task", "text": "do rep work", "status": "queued", "team_id": "team1"},
    ])
    client.get_ai_profile = profile_client.get_ai_profile
    client.update_ai_profile = profile_client.update_ai_profile
    return client, profile_client


def _deep_task_provider():
    """A provider whose first plan chooses a deep run (so a rep_preamble reaches the deep runner)."""
    return StubProvider(decisions=[
        {"action": "deep", "goal": "do the work", "deep_brief": "x", "rationale": "work"},
        {"met": True, "reason": "did it"},  # goal verification
    ])


# --- (a) pull => rep persona is rendered AND injected into the deep run ----------

def test_pull_injects_rep_persona_into_deep_run():
    client, profile_client = _profile_aware_client()
    deep = CapturingDeepRunner(met=True, output="did it")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=_deep_task_provider(),
            deep_runner=deep,
            rep_sync_resolver=lambda task: ("u1", d),
            rep_sync_direction="pull",
        )
        poller = Poller(cfg, state_path=None, client=client)
        # The orchestrator the poller builds must use OUR capturing deep runner.
        poller._orchestrator = _brain(cfg.model_provider, deep)
        handled = poller.run_once()
        skill_text = Path(d, "SKILL.md").read_text()

    assert handled == ["rep-task"]
    # The profile was pulled (Quest is the source of truth at execution time) and rendered.
    assert profile_client.get_calls == [("team1", "u1")]
    assert "You are decisive and concise." in skill_text
    # The deep run received a rep_preamble carrying that persona + a learned correction.
    assert deep.calls, "the deep run must have been invoked"
    preamble = deep.calls[0]["context_preamble"]
    assert preamble is not None
    assert "You are decisive and concise." in preamble
    assert "be concise in status updates" in preamble


def test_pull_default_direction_still_injects_persona():
    """rep_sync_direction defaults to 'pull', so a consumer that only sets the resolver gets the
    persona in the deep run with NO direction config at all."""
    client, _ = _profile_aware_client()
    deep = CapturingDeepRunner()
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=_deep_task_provider(),
            deep_runner=deep,
            rep_sync_resolver=lambda task: ("u1", d),
            # rep_sync_direction left at its default
        )
        assert cfg.rep_sync_direction == "pull"
        poller = Poller(cfg, state_path=None, client=client)
        poller._orchestrator = _brain(cfg.model_provider, deep)
        poller.run_once()
    assert deep.calls and deep.calls[0]["context_preamble"] is not None
    assert "You are decisive and concise." in deep.calls[0]["context_preamble"]


# --- (b) direction="both" => a push fires AFTER execute --------------------------

def test_direction_both_pulls_then_pushes():
    client, profile_client = _profile_aware_client()
    deep = CapturingDeepRunner()
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=_deep_task_provider(),
            deep_runner=deep,
            rep_sync_resolver=lambda task: ("u1", d),
            rep_sync_direction="both",
        )
        poller = Poller(cfg, state_path=None, client=client)
        poller._orchestrator = _brain(cfg.model_provider, deep)
        handled = poller.run_once()
    assert handled == ["rep-task"]
    # Pulled before the run AND pushed back after it.
    assert profile_client.get_calls == [("team1", "u1")]
    assert len(profile_client.updates) == 1
    assert profile_client.updates[0]["user_id"] == "u1"
    # The run still got the rep persona (both pulls first).
    assert deep.calls and deep.calls[0]["context_preamble"] is not None


def test_direction_push_only_pushes_and_does_not_pull():
    """'push' means push-after-run ONLY: no pre-run pull, so no rep_preamble is injected."""
    client, profile_client = _profile_aware_client()
    deep = CapturingDeepRunner()
    with tempfile.TemporaryDirectory() as d:
        # Seed a skill file so push has something to read (push without a file would error,
        # best-effort, and is logged — but here we prove the push path runs cleanly).
        from quest_ai_runner.runner.rep_sync import pull_rep_to_skill
        pull_rep_to_skill(client, "team1", "u1", d)
        profile_client.get_calls.clear()  # reset: the seed pull is not part of the scan
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=_deep_task_provider(),
            deep_runner=deep,
            rep_sync_resolver=lambda task: ("u1", d),
            rep_sync_direction="push",
        )
        poller = Poller(cfg, state_path=None, client=client)
        poller._orchestrator = _brain(cfg.model_provider, deep)
        poller.run_once()
    # No pre-run pull happened during the scan (push-only direction).
    assert profile_client.get_calls == []
    # A push DID fire after the run.
    assert len(profile_client.updates) == 1
    # No rep_preamble was injected (nothing was pulled to build it from).
    assert deep.calls and deep.calls[0]["context_preamble"] is None


# --- (c) NO resolver => unchanged path (no pull, no push, no rep_preamble) -------

def test_no_resolver_no_sync_no_preamble():
    client, profile_client = _profile_aware_client()
    deep = CapturingDeepRunner()
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=_deep_task_provider(),
        deep_runner=deep,
        # no rep_sync_resolver
    )
    poller = Poller(cfg, state_path=None, client=client)
    poller._orchestrator = _brain(cfg.model_provider, deep)
    handled = poller.run_once()
    assert handled == ["rep-task"]
    assert profile_client.get_calls == []         # no pull
    assert profile_client.updates == []           # no push
    assert deep.calls and deep.calls[0]["context_preamble"] is None  # no rep preamble


def test_executor_rep_preamble_is_threaded_into_the_deep_run():
    """Direct executor seam: execute(task, rep_preamble=...) lands as the deep run's
    context_preamble; omitting it leaves the deep run's preamble None (unchanged)."""
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(_deep_task_provider(), deep))
    ex.execute({"id": "t-rep", "text": "do work"}, rep_preamble="ACT AS REP\npersona body")
    assert deep.calls[0]["context_preamble"] == "ACT AS REP\npersona body"

    deep2 = CapturingDeepRunner()
    ex2 = TaskExecutor(MockQuestClient([]), _brain(_deep_task_provider(), deep2))
    ex2.execute({"id": "t-norep", "text": "do work"})  # no rep_preamble
    assert deep2.calls[0]["context_preamble"] is None


# --- (d) unknown direction is reported by validate() ----------------------------

def test_validate_reports_unknown_direction():
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[]),
        rep_sync_direction="sideways",
    )
    problems = cfg.validate()
    assert any("rep_sync_direction" in p for p in problems)
    # A valid direction yields no such problem.
    cfg.rep_sync_direction = "both"
    assert not any("rep_sync_direction" in p for p in cfg.validate())


# --- (e) the TASK DOCUMENT can supply its own persona (rep_preamble field) -------

def _poller_with(client, deep, provider=None, **cfg_kw):
    """A Poller wired to OUR capturing deep runner (the brain the poller builds is replaced)."""
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=provider or _deep_task_provider(),
        deep_runner=deep,
        **cfg_kw,
    )
    poller = Poller(cfg, state_path=None, client=client)
    poller._orchestrator = _brain(cfg.model_provider, deep)
    return poller


def test_task_supplied_rep_preamble_is_used_when_no_rep_resolves():
    """A task carrying ``rep_preamble`` and resolving to NO rep runs (and reports) in that voice.

    This is the deferred-from-a-conversation case: the queueing side stamps the conversation's own
    persona on the task, so the deep run acts as it AND the fold-back done report speaks as it.
    """
    persona = "You are Ada, a calm, plain-spoken assistant."
    client = MockQuestClient([
        {"id": "chat-task", "text": "do the deferred work", "status": "queued",
         "team_id": "team1", "rep_preamble": persona},
    ])
    deep = CapturingDeepRunner(met=True, output="raw worker transcript tail")
    poller = _poller_with(client, deep)  # no rep_sync_resolver: no rep resolves for this task

    # Spy on the fold-back synthesis so we can assert the persona reaches the DONE REPORT too.
    seen = {}

    def fake_synthesize(request, raw_summary, *, rep_preamble=None, **_kw):
        seen["rep_preamble"] = rep_preamble
        return "Done. Here is what I found, in Ada's voice."

    poller._orchestrator.synthesize_task_report = fake_synthesize
    poller._orchestrator.report_claims_unbacked = lambda *a, **kw: False

    handled = poller.run_once()

    assert handled == ["chat-task"]
    # 1) the deep run executed AS that persona
    assert deep.calls and deep.calls[0]["context_preamble"] == persona
    # 2) the fold-back done report was composed in the same voice, and is what was reported
    assert seen["rep_preamble"] == persona
    assert client.reports[0][:3] == ("chat-task", "done",
                                     "Done. Here is what I found, in Ada's voice.")


def test_resolved_rep_wins_over_a_task_supplied_rep_preamble():
    """Precedence: an explicitly resolved rep persona beats the task document's field."""
    client, profile_client = _profile_aware_client()
    client._due[0]["rep_preamble"] = "You are Ada, a calm, plain-spoken assistant."
    deep = CapturingDeepRunner()
    with tempfile.TemporaryDirectory() as d:
        poller = _poller_with(client, deep,
                              rep_sync_resolver=lambda task: ("u1", d),
                              rep_sync_direction="pull")
        handled = poller.run_once()

    assert handled == ["rep-task"]
    preamble = deep.calls[0]["context_preamble"]
    assert "You are decisive and concise." in preamble   # the rep's pulled persona
    assert "Ada" not in preamble                          # the task field was NOT used


def test_non_string_or_empty_task_rep_preamble_is_ignored():
    """Anything that is not a non-empty string is ignored (no persona, no crash)."""
    for value in (123, "", "   ", {"persona": "x"}, ["x"], None, True):
        client = MockQuestClient([
            {"id": "t-bad", "text": "do work", "status": "queued", "team_id": "team1",
             "rep_preamble": value},
        ])
        deep = CapturingDeepRunner()
        poller = _poller_with(client, deep)
        handled = poller.run_once()
        assert handled == ["t-bad"], f"the task must still run for rep_preamble={value!r}"
        assert deep.calls[0]["context_preamble"] is None, f"rep_preamble={value!r} leaked through"


def test_older_deep_runner_without_context_preamble_is_untouched():
    """A DeepRunner whose run_goal does NOT accept context_preamble (the conftest stub) must keep
    working even when a rep_preamble is supplied — the orchestrator simply does not forward it."""
    from .conftest import StubDeepRunner

    deep = StubDeepRunner(met=True, output="ok")
    ex = TaskExecutor(MockQuestClient([]), _brain(_deep_task_provider(), deep))
    out = ex.execute({"id": "t-old", "text": "do work"}, rep_preamble="some persona")
    assert out.status == "done"
    # The stub recorded a call with no context_preamble kwarg (it never accepted one).
    assert deep.calls and "context_preamble" not in deep.calls[0]
