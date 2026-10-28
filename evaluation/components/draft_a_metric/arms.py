"""Shared machinery of the arms: the fake Quest client at the boundary and one runner of the real ``MetricDraft`` over a case."""
import common  # noqa: F401  (puts the repository on the path)
from quest_ai_runner.runner import metric_draft

TASK_ID = "task_1"


class FakeClient:
    """The Quest client at the boundary: serves the case's metric card and records the proposal the draft raises."""

    def __init__(self, card):
        self.card, self.proposals = card, []

    def get_metric_card(self, collection_id):  # noqa: ARG002
        return self.card

    def propose_metric_value(self, collection_id, values, evidence, **kwargs):
        self.proposals.append({"collection_id": collection_id, "values": values, "evidence": evidence, **kwargs})
        return {"decisionId": "teamdec_eval"}


def task_of(case):
    return {"id": TASK_ID, "source": metric_draft.SOURCE, "quest_id": "quest_1", "goal_id": "quest_1", "text": case["task_text"]}


def run_draft(case, provider, model="pinned"):
    """The real draft turn with ``provider`` (or none: the arm that asks the owner makes no turn at all)."""
    client = FakeClient(case["card"])
    if provider is None:
        return {"status": "nothing", "values": None, "evidence": [], "model_calls": 0, "text": "the owner is asked to write the value"}
    turn = metric_draft.MetricDraft(client, provider, model)
    result = turn.run(task_of(case), TASK_ID, "quest_1", "")
    sent = client.proposals[0] if client.proposals else {}
    return {"status": result["status"], "values": sent.get("values"), "evidence": sent.get("evidence") or [],
            "model_calls": turn.model_calls, "text": result.get("text", "")}


class ScriptedProvider:
    """A provider whose one ``plan`` answer is scripted: the floor and the baseline use it so the real draft code still runs."""

    def __init__(self, decision):
        self.decision = decision

    def plan(self, prompt, *, model, tool_schema, layers=None):  # noqa: ARG002
        return self.decision
