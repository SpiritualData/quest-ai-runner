"""Link guard wired into the orchestrator: the reply that leaves is the sanitized one. The guard itself (hubofhubs.critique.link_guard)
is tested in the hub; every test here is offline."""
from __future__ import annotations

from hubofhubs.critique.link_guard import DEAD, OK, LinkGuard, LinkPolicy


def fetcher_for(statuses):
    """A fake probe returning a canned (status, detail) per URL, and counting the calls."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return statuses.get(url, (None, "timeout"))

    fetch.calls = calls
    return fetch


APP = LinkPolicy(
    routes=("/profile/ai-tasks", "/quest/[questId]/chat", "/quest", "/events/[slug]/[...rest]"),
    origins=("app.example.org",),
    trusted_hosts=("docs.example.org",),
    allowed_schemes=("app-task",),
    passes=2,
)


# --- route matching ---------------------------------------------------------------------------

# --- wired into the orchestrator: the reply that LEAVES is the sanitized one -----------------------

def test_a_finished_answer_has_its_fabricated_link_stripped_before_it_is_emitted():
    """End to end through Orchestrator.run: the guard is the last thing to touch the words."""
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = ("Open [your tasks](/profile/ai-tasks), then check "
             "[the settings hub](/settings/hub).")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    # Inject the guard directly: the policy file / env plumbing has its own tests above, and this
    # one is about finish() applying whatever guard it resolves.
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(APP, fetcher=fetcher_for({}))

    res = orch.run("where are my tasks?")

    assert res.kind == "answer"
    assert "/profile/ai-tasks" in res.text
    assert "/settings/hub" not in res.text
    assert "the settings hub (link removed: that address does not exist)" in res.text
    assert {c["url"]: c["verdict"] for c in res.link_checks} == {
        "/profile/ai-tasks": OK, "/settings/hub": DEAD}


def test_the_guard_can_be_switched_off_and_then_the_reply_is_untouched():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = "Check [the settings hub](/settings/hub)."
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.link_guard = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    res = orch.run("where?")
    assert "/settings/hub" in res.text
    assert res.link_checks == []
PROV = LinkPolicy(
    routes=("/quest/[questId]", "/profile/ai-tasks"),
    origins=("app.example.org",),
    passes=1,
    require_provenance=True,
)
REACHABLE = {
    "https://seen.example.com/page": (200, "HEAD"),
    "https://invented.example.com/page": (200, "HEAD"),
}


def test_the_orchestrator_hands_the_turns_own_material_to_the_guard():
    """End to end: a link from the user's message and one from a gathered read pass; an invented
    one is stripped. The evidence comes from the run, not from a global."""
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = ("From you: [one](https://seen.example.com/page). Invented: "
             "[two](https://invented.example.com/page). Mine: [q](/quest/abc).")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(PROV, fetcher=fetcher_for(REACHABLE))

    res = orch.run("please summarize https://seen.example.com/page for me")

    assert "https://seen.example.com/page" in res.text
    assert "invented.example.com" not in res.text
    assert "/quest/abc" in res.text


def test_a_policy_object_on_the_config_builds_the_guard_without_a_file():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}], answer_text="hi")
    cfg = OrchestratorConfig()
    cfg.link_policy = PROV
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    guard = orch.resolve_link_guard()
    assert guard is not None and guard.policy is PROV


# --- provenance for deep runs: model output is never evidence ----------------------------------

def deep_turn(runner, message="show me my docs"):
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .test_deep_run_status_honesty import deep_plan
    from .test_per_goal_context_iteration import ScriptedProvider
    from .conftest import StubRetrieval

    provider = ScriptedProvider(plans=[deep_plan()], verdicts=[{"met": True}])
    cfg = OrchestratorConfig(deep_model_ladder=["haiku", "sonnet", "opus"])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner, config=cfg)
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(
        LinkPolicy(passes=1, require_provenance=True),
        fetcher=fetcher_for({"https://seen.example.com/a": (200, "HEAD"),
                             "https://invented.example.com/b": (200, "HEAD")}))
    return orch.run(message)


def test_an_in_process_runners_invented_link_is_stripped_and_cannot_vouch_for_itself():
    from hubofhubs.ports.deep import DeepResult

    from .test_deep_run_status_honesty import ScriptedRunner

    out = ("Docs: [seen](https://seen.example.com/a) and [made up](https://invented.example.com/b)")
    runner = ScriptedRunner([DeepResult(met=True, output=out,
                                        read_evidence="row: https://seen.example.com/a")],
                            uses_model=False)
    res = deep_turn(runner)
    text = res.deep_results[0].output
    assert res.deep_results[0].model_written is True
    assert "https://seen.example.com/a" in text      # what the runner itself read survives
    assert "invented.example.com" not in text        # its own words never vouch for a link


def test_a_real_claude_code_workers_output_keeps_its_exemption():
    from hubofhubs.ports.deep import DeepResult

    from .test_deep_run_status_honesty import ScriptedRunner

    runner = ScriptedRunner([DeepResult(met=True, output="[b](https://invented.example.com/b)")])
    res = deep_turn(runner)
    assert res.deep_results[0].model_written is False
    assert "https://invented.example.com/b" in res.deep_results[0].output


def test_link_evidence_excludes_model_written_deep_output_but_keeps_what_it_read():
    from types import SimpleNamespace

    from hubofhubs.ports.deep import DeepResult
    from quest_ai_runner.core.orchestrator_judges import JudgesMixin

    res = SimpleNamespace(deep_results=[
        DeepResult(met=True, output="https://model.example.com/x", model_written=True,
                   read_evidence="https://read.example.com/y"),
        DeepResult(met=True, output="https://worker.example.com/z"),
    ])
    rs = SimpleNamespace(user_message="u", transcript="", context_view="", gathered=None,
                         attachments=None, rep_preamble="persona https://persona.example.com/p",
                         context_meta={"link_evidence": ["https://extra.example.com/e"]})
    text = JudgesMixin.link_evidence_for(SimpleNamespace(), rs, res)
    assert "model.example.com" not in text
    for kept in ("read.example.com", "worker.example.com", "persona.example.com", "extra.example.com"):
        assert kept in text
