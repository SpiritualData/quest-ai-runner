"""A DOI in a finished reply that the turn did not have is replaced; the link guard alone lets a bare invented DOI through.
Offline: a scripted provider, a fake OpenAlex transport."""
from types import SimpleNamespace

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.tools_science import TOOL_NAME, reset_shared_state

from .conftest import StubProvider, StubRetrieval
from .test_tools_science import WORKS, Response, Transport, registry_with

REAL = "10.5555/demo.one"
INVENTED = "10.5555/made.up"


def run_turn(answer_text, message="What do the papers say about cycling after a knee injury? Cite two."):
    reset_shared_state()
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "search", "tool_calls": [{"name": TOOL_NAME, "args": {"query": "cycling knee"}}]},
        {"action": "answer", "rationale": "cite what came back"}], answer_text=answer_text)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg,
                        tools=registry_with(Transport(Response(WORKS))))
    return orch.run(message)


def test_an_invented_bare_doi_is_replaced_and_the_returned_one_is_kept():
    res = run_turn(f"Cycling helped (Author One 2021, DOI {REAL}). Another trial (DOI {INVENTED}) agrees.")
    assert res.kind == "answer"
    assert f"DOI {REAL}" in res.text and INVENTED not in res.text and "[DOI not verified]" in res.text
    assert {"url": f"doi:{INVENTED}", "verdict": "unknown", "reason": "DOI not in this turn's evidence"} in res.link_checks


def test_a_reply_that_cites_only_returned_dois_is_untouched():
    res = run_turn(f"Cycling helped (Author One 2021, doi:{REAL.upper()}).")
    assert "[DOI not verified]" not in res.text and REAL.upper() in res.text


def test_a_doi_the_person_pasted_is_evidence_too():
    res = run_turn(f"You gave {INVENTED}; I could not find it in the search.", message=f"Is {INVENTED} a real paper? Search for it.")
    assert INVENTED in res.text


def test_the_check_is_part_of_the_link_guard_and_goes_with_it():
    reset_shared_state()
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}], answer_text=f"See DOI {INVENTED}.")
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.link_guard = False
    res = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg).run("hello there")
    assert INVENTED in res.text


def test_a_failure_inside_the_check_never_loses_the_reply():
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=StubProvider([]), registry=ModelRegistry(StubProvider([])))
    res = SimpleNamespace(text=f"DOI {INVENTED}", deep_results=[], link_checks=[])
    orch.link_evidence_for = lambda rs, r: (_ for _ in ()).throw(RuntimeError("boom"))
    assert orch.strip_unverified_dois(SimpleNamespace(), res) == [] and res.text == f"DOI {INVENTED}"
