"""A metric draft is one light turn, never a deep run.

A task with ``source == "metric_values"`` asks for a draft value of one metric. Everything the draft needs is assembled up front (the metric
card plus the quest's context view), so the executor makes ONE structured model call, raises the proposal as an ask, and never opens the
orchestrator or a deep runner. These tests pin that: no deep run or orchestrator call, a proposal built from the assembled context, thin
evidence proposing nothing, and a refusal or a provider error ending the task honestly.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.runner import metric_draft
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.runner.quest_client import QuestApiError
from tests.conftest import StubDeepRunner

CARD = {"name": "Weekly distance", "purpose": "Ensure the weekly running distance is known", "disadvantages": "Cannot see pace or effort",
        "questions": [{"name": "Kilometres", "type": "number", "min": None, "max": None}],
        "period": {"start": "2026-10-19", "end": "2026-10-25"}, "recentValues": [{"value": 18.0, "at": "2026-10-18T09:00:00"}],
        "notesSinceBaseline": [{"at": "2026-10-20", "text": "Ran 12 km on Tuesday"}], "otherEntriesInPeriod": [],
        "goalsSinceBaseline": [{"at": "2026-10-22", "name": "Run 6 km Friday", "status": "completed", "target": ""}],
        "answeredAsksSinceBaseline": [{"at": "2026-10-23", "question": "Did you run Friday?", "answer": "Yes, 6 km"}],
        "outcome": {"text": "Run 40 km in a week"}, "hasNewContext": True}
TASK = {"id": "task_1", "source": "metric_values", "quest_id": "quest_1", "goal_id": "quest_1",
        "text": 'Draft a value for the metric "Weekly distance" (metric collection coll_1) for 2026-10-19 to 2026-10-25.'}
PROPOSE = {"propose": True, "values": {"Kilometres": 18}, "confidence": "medium",
           "evidence": [{"kind": "note", "summary": "Ran 12 km on Tuesday", "date": "2026-10-20"},
                        {"kind": "goal", "summary": "Run 6 km Friday completed", "date": "2026-10-22"}]}


class Client:
    def __init__(self, refuse=None):
        self.refuse, self.card_reads, self.proposals, self.done, self.failed = refuse, [], [], [], []

    def get_metric_card(self, collection_id):
        self.card_reads.append(collection_id)
        return CARD

    def propose_metric_value(self, collection_id, values, evidence, **kwargs):
        if self.refuse:
            raise QuestApiError(self.refuse)
        self.proposals.append((collection_id, values, evidence, kwargs))
        return {"decisionId": "teamdec_9"}

    def report_progress(self, *args, **kwargs):
        return None

    def report_done(self, task_id, text):
        self.done.append((task_id, text))

    def report_failed(self, task_id, text):
        self.failed.append((task_id, text))


class Provider:
    """plan() answers the one structured call; answer() must never be used."""

    def __init__(self, decision=None, raises=None):
        self.decision, self.raises, self.prompts, self.schemas = decision, raises, [], []

    def plan(self, prompt, *, model, tool_schema, layers=None):
        self.prompts.append(prompt)
        self.schemas.append(tool_schema)
        if self.raises:
            raise self.raises
        return self.decision

    def answer(self, *args, **kwargs):
        raise AssertionError("a metric draft makes one structured call, not an answer")

    def list_models(self):
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


def executor(client, provider):
    deep = StubDeepRunner()

    def never_run(*args, **kwargs):
        raise AssertionError("a metric draft must not go through the orchestrator or open a deep run")

    orchestrator = SimpleNamespace(registry=ModelRegistry(provider), provider=provider, deep_runner=deep, run=never_run, retrieval=None)
    return TaskExecutor(client, orchestrator), deep


def test_a_draft_is_one_structured_call_from_the_card_and_context_and_opens_no_deep_run():
    client, provider = Client(), Provider(PROPOSE)
    ex, deep = executor(client, provider)
    outcome = ex.execute(dict(TASK))
    assert outcome.status == "done" and outcome.decision_id == "teamdec_9"
    assert deep.calls == [] and len(provider.prompts) == 1
    collection, values, evidence, kwargs = client.proposals[0]
    assert collection == "coll_1" and values == {"Kilometres": 18} and len(evidence) == 2 and kwargs["period_start"] == "2026-10-19"
    assert client.done and "teamdec_9" in client.done[0][1] and client.failed == []


def test_the_prompt_carries_what_the_draft_needs_so_no_deep_run_is_needed():
    client, provider = Client(), Provider(PROPOSE)
    ex, _ = executor(client, provider)
    ex.execute(dict(TASK))
    prompt = provider.prompts[0]
    for needed in ("Ran 12 km on Tuesday", "Run 6 km Friday", "Did you run Friday?", "Yes, 6 km", "Weekly distance", "Kilometres", "Run 40 km in a week", "Cannot see pace or effort"):
        assert needed in prompt
    assert provider.schemas[0]["name"] == "metric_value_draft"


def test_thin_evidence_proposes_nothing_and_the_task_is_still_done():
    client, provider = Client(), Provider({"propose": False, "reason": "no notes or entries since the last update"})
    ex, deep = executor(client, provider)
    outcome = ex.execute(dict(TASK))
    assert outcome.status == "done" and client.proposals == [] and deep.calls == []
    assert "No value proposed" in outcome.result and "no notes or entries" in outcome.result


@pytest.mark.parametrize("decision", [
    {"propose": True, "values": {"Kilometres": 18}, "evidence": []},
    {"propose": True, "values": {}, "evidence": [{"summary": "Ran 12 km on Tuesday"}]},
    {"propose": True, "values": {"Kilometres": 18}, "evidence": [{"summary": "ran"}]},
    None, "unreadable"])
def test_a_proposal_without_values_or_specific_evidence_is_never_sent(decision):
    client, provider = Client(), Provider(decision)
    ex, _ = executor(client, provider)
    outcome = ex.execute(dict(TASK))
    assert outcome.status == "done" and client.proposals == []


def test_a_refusal_from_quest_fails_the_task_with_its_reason():
    client, provider = Client(refuse="Not done: this metric does not allow the Quest AI Runner"), Provider(PROPOSE)
    ex, deep = executor(client, provider)
    outcome = ex.execute(dict(TASK))
    assert outcome.status == "failed" and "does not allow" in outcome.result and client.failed and deep.calls == []


def test_a_provider_error_fails_the_task_and_never_loops():
    client, provider = Client(), Provider(raises=RuntimeError("model unavailable"))
    ex, deep = executor(client, provider)
    outcome = ex.execute(dict(TASK))
    assert outcome.status == "failed" and "model unavailable" in outcome.result and len(provider.prompts) == 1 and deep.calls == []


def test_a_task_that_names_no_metric_fails_without_a_model_call():
    client, provider = Client(), Provider(PROPOSE)
    ex, _ = executor(client, provider)
    outcome = ex.execute({**TASK, "text": "Draft a value for a metric"})
    assert outcome.status == "failed" and provider.prompts == []


def test_only_the_metric_values_source_takes_the_light_path():
    assert metric_draft.is_metric_draft({"source": "metric_values"}) and not metric_draft.is_metric_draft({"source": "chat"})
    assert metric_draft.collection_id_of({"metric_collection_id": "coll_2", "text": "x"}) == "coll_2"
    assert metric_draft.collection_id_of(TASK) == "coll_1"
