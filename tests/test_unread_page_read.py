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
