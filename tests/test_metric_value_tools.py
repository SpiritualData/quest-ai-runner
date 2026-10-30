"""Metric values as registry tools: a run reads a metric's card and proposes a value, and never writes one.

The person confirms a proposal, so the only write the runner can make is an ask: these tests pin that the tool sends the evidence and the values
to the proposal route and reports the value as proposed, not recorded, and that a refusal names what to change.
"""
import pytest

from quest_ai_runner.core import tools_metric_values as metric_tools
from quest_ai_runner.core.tools import STANDARD_TOOL_BUILDERS, ToolContext, ToolRegistry
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient

CREDS = {"QUEST_BASE_URL": "https://quest.example", "QUEST_API_KEY": "key"}
CARD = {"name": "Weekly distance", "questions": [{"name": "Kilometres", "type": "number", "min": None, "max": None},
                                                 {"name": "Effort", "type": "rating", "min": 1, "max": 5}],
        "recentValues": [{"value": 18.0, "at": "2026-10-18T09:00:00"}], "notesSinceBaseline": [{"text": "Ran twice"}],
        "otherEntriesInPeriod": [], "hasNewContext": True, "outcome": {"text": "Run 40 km in a week"}}


class FakeClient:
    def __init__(self, refuse=None):
        self.calls, self.refuse = [], refuse

    def get_metric_card(self, collection_id):
        self.calls.append(("card", collection_id))
        return CARD

    def propose_metric_value(self, collection_id, values, evidence, **kwargs):
        self.calls.append(("propose", collection_id, values, evidence, kwargs))
        if self.refuse:
            raise QuestApiError(self.refuse)
        return {"decisionId": "teamdec_1"}


def run(builder, client, args, quest_id="quest_1"):
    spec = builder(CREDS, client_factory=lambda: client)
    return ToolRegistry([spec]).invoke(spec.name, args, ToolContext(quest_id=quest_id))


def test_both_tools_are_in_the_standard_catalog_only_with_credentials():
    assert {"get_metric_card", "propose_metric_value"} <= set(STANDARD_TOOL_BUILDERS)
    assert STANDARD_TOOL_BUILDERS["get_metric_card"]({}) is None and STANDARD_TOOL_BUILDERS["propose_metric_value"]({}) is None
    assert STANDARD_TOOL_BUILDERS["propose_metric_value"](CREDS).mutates is True and STANDARD_TOOL_BUILDERS["get_metric_card"](CREDS).mutates is False


def test_the_card_reads_in_one_line_and_needs_the_collection():
    client = FakeClient()
    result = run(metric_tools.get_metric_card_spec, client, {"collection_id": "coll_1"})
    assert result.ok and client.calls == [("card", "coll_1")]
    assert "Kilometres (number)" in result.text and "Effort (rating, 1 to 5)" in result.text and "18 on 2026-10-18" in result.text
    assert "New context on the quest: yes" in result.text
    assert run(metric_tools.get_metric_card_spec, client, {}).ok is False


def test_a_proposal_is_sent_with_its_evidence_and_reported_as_proposed_not_recorded():
    client = FakeClient()
    evidence = [{"kind": "note", "summary": "Ran 12 km on Tuesday and 6 km on Friday", "date": "2026-10-20"}]
    result = run(metric_tools.propose_metric_value_spec, client, {
        "collection_id": "coll_1", "values": {"Kilometres": 18, "Effort": 3}, "evidence": evidence, "confidence": "low",
        "confidence_reason": "two notes", "could_not_see": "the watch data"})
    assert result.status.value == "held" and "teamdec_1" in result.text and "Do not report the value as recorded" in result.text
    call = client.calls[0]
    assert call[1:4] == ("coll_1", {"Kilometres": 18, "Effort": 3}, evidence)
    assert call[4]["confidence"] == "low" and call[4]["confidence_reason"] == "two notes" and call[4]["could_not_see"] == "the watch data"


def test_a_call_without_values_or_evidence_is_refused_before_the_network_and_a_backend_refusal_says_why():
    client = FakeClient(refuse="Not done: The person has not allowed the Quest AI Runner to propose values for this metric")
    for bad in ({"collection_id": "c"}, {"collection_id": "c", "values": {"K": 1}}, {"collection_id": "c", "values": {}, "evidence": [{"summary": "x y z"}]},
                {"values": {"K": 1}, "evidence": [{"summary": "x y z"}]}):
        refused = run(metric_tools.propose_metric_value_spec, client, bad)
        assert refused.ok is False and "evidence" in refused.text or "collection_id" in refused.text
    assert client.calls == []
    refused = run(metric_tools.propose_metric_value_spec, client, {"collection_id": "c", "values": {"K": 1}, "evidence": [{"summary": "Ran 6 km on Friday"}]})
    assert refused.ok is False and "has not allowed" in refused.text


def test_the_client_methods_use_the_metric_values_routes(monkeypatch):
    client = QuestClient.__new__(QuestClient)
    seen = []

    def request(method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return {"decisionId": "teamdec_9"}
    client._request = request
    client.get_metric_card("coll_9")
    client.propose_metric_value("coll_9", {"K": 1}, [{"summary": "Ran 6 km on Friday"}], period_start="2026-10-19")
    assert seen[0] == ("GET", "/api/metric-values/coll_9/card", None)
    assert seen[1][:2] == ("POST", "/api/metric-values/proposals") and seen[1][2]["collectionId"] == "coll_9" and seen[1][2]["periodStart"] == "2026-10-19"
