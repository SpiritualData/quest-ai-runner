"""A verdict that names an address the turn only saw in search results leads to one cheap page read, not a deep run.

Driven by the verdict's structured ``unread_pages`` field. A not-met verdict without it behaves as before.
"""
from __future__ import annotations

from typing import Any, List

from quest_ai_runner.core.adapters_values import Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_guard import GuardProvider, SucceedingDeepRunner

URL = "https://releases.example/latest"
FIRST = "Version 9.9 is the latest."
SECOND = "Version 9.9 is the latest, released on a stated date: " + URL


class FakeWeb:
    backend_name = "fake"

    def __init__(self) -> None:
        self.fetched: List[str] = []

    def search(self, queries: Any, **kw: Any) -> Observation:
        return Observation(kind="query", rel_path="web_search:x", text="WEB RESULTS", hits=[])

    def fetch(self, url: str, **kw: Any) -> Observation:
        self.fetched.append(url)
        return Observation(kind="read", rel_path=url, locator=f"web extract: {url}", text="Release page: 9.9 on a stated date.")


class PageProvider(GuardProvider):
    def answer(self, messages, *, model, system=None) -> str:
        joined = "\n".join(m["content"] if isinstance(m["content"], str) else "" for m in messages)
        if "cited but never opened" in joined:
            return SECOND
        return super().answer(messages, model=model, system=system)


def run(provider, web):
    runner = SucceedingDeepRunner()
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(), deep_runner=runner, web=web)
    return orch.run("latest version and its release page?"), runner


def test_unread_page_is_read_then_met_without_a_deep_run():
    provider = PageProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        goal_verdicts=[{"met": False, "reason": "link not opened", "unread_pages": [URL, URL]}, {"met": True}],
        answer_text=FIRST)
    web = FakeWeb()
    res, runner = run(provider, web)
    assert web.fetched == [URL]
    assert runner.calls == 0
    assert res.text.startswith("Version 9.9 is the latest, released")   # the rewrite; the link guard then judges the address itself
    assert provider.verify_calls == 2
    assert res.exit_reason == "verified"


def test_not_met_without_the_field_behaves_as_before():
    provider = PageProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        goal_verdicts=[{"met": False, "reason": "thin", "next_action": "say more"}, {"met": False}],
        answer_text=FIRST)
    web = FakeWeb()
    res, runner = run(provider, web)
    assert web.fetched == []
    assert runner.calls == 1          # the last-resort deep run, as before
    assert provider.verify_calls == 1


def test_an_address_is_read_once_even_if_the_verifier_names_it_again():
    again = {"met": False, "reason": "still", "unread_pages": [URL]}
    provider = PageProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        goal_verdicts=[again, again, {"met": True}], answer_text=FIRST)
    web = FakeWeb()
    run(provider, web)
    assert web.fetched == [URL]


class FailingWeb(FakeWeb):
    def fetch(self, url: str, **kw: Any) -> Observation:
        self.fetched.append(url)
        return Observation(kind="error", error="page did not load")


def test_a_page_that_cannot_be_opened_ends_with_an_honest_sentence_and_no_deep_run():
    provider = PageProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        goal_verdicts=[{"met": False, "reason": "link not opened", "unread_pages": [URL]}, {"met": True}],
        answer_text=FIRST)
    web = FailingWeb()
    res, runner = run(provider, web)
    assert web.fetched == [URL]
    assert runner.calls == 0
    assert res.text.startswith(FIRST) and "could not open the page at releases.example" in res.text
    assert provider.verify_calls == 1


def test_failed_reads_are_found_from_the_error_observations_alone():
    from quest_ai_runner.core.link_provenance_pass import failed_page_reads
    assert failed_page_reads([{"kind": "error", "rel_path": URL, "error": "x"}, {"kind": "read", "rel_path": "https://ok.example/"}])
    assert not failed_page_reads([{"kind": "read", "rel_path": URL}, {"kind": "error", "error": "no address"}])


def test_planner_web_block_tells_the_planner_to_open_a_requested_official_page():
    from quest_ai_runner.core.planner_context import PLANNER_WEB_HEAD, planner_web_block
    assert "official page or a link to a source" in PLANNER_WEB_HEAD
    assert "one web_page read before you answer" in PLANNER_WEB_HEAD
    assert "—" not in PLANNER_WEB_HEAD
    assert "official page" in planner_web_block(FakeWeb())      # rides the block whenever a web adapter is wired
    assert planner_web_block(None) == ""


def test_an_unconfirmed_reply_stops_with_its_own_reason_and_a_real_out_of_tries_stop_keeps_its_own():
    from types import SimpleNamespace
    from quest_ai_runner.core.orchestrator_turn_messages import turn_outcome
    unconfirmed = SimpleNamespace(deep_results=[], goal_verdict={"met": False, "blocker": "evidence_only"})
    assert turn_outcome(unconfirmed) == {"outcome": "stopped", "stop_reason": "unverified"}
    more_work = SimpleNamespace(deep_results=[], goal_verdict={"met": False, "blocker": "more_work"})
    assert turn_outcome(more_work) == {"outcome": "stopped", "stop_reason": "attempt_cap"}
    assert turn_outcome(SimpleNamespace(deep_results=[], goal_verdict={"met": True})) == {"outcome": "met"}


def test_an_evidence_only_verdict_ships_the_answer_with_no_deep_run_and_the_unverified_stop_reason():
    from quest_ai_runner.core.orchestrator_turn_messages import turn_outcome
    provider = PageProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        goal_verdicts=[{"met": False, "blocker": "evidence_only", "reason": "cited page is a home page",
                        "question": "whether a dedicated release page exists"}, {"met": True}],
        answer_text=FIRST)
    res, runner = run(provider, FakeWeb())
    assert runner.calls == 0
    assert res.text.startswith(FIRST)
    assert provider.verify_calls == 1
    assert turn_outcome(res) == {"outcome": "stopped", "stop_reason": "unverified"}


def test_evidence_only_without_a_question_or_more_work_behaves_as_before():
    for verdict in ({"met": False, "blocker": "evidence_only"}, {"met": False, "blocker": "more_work", "question": "q"}):
        provider = PageProvider(plan_decisions=[{"action": "answer", "rationale": "x"}],
                                goal_verdicts=[verdict, {"met": False}], answer_text=FIRST)
        res, runner = run(provider, FakeWeb())
        assert runner.calls == 1          # the last-resort deep run, unchanged
