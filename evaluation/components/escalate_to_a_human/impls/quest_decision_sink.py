"""CURRENT: the production ``QuestDecisionSink`` (``runner/quest_client.py``) with an in-memory fake Quest
client at the network edge. Zero model calls, zero network."""
import os
from datetime import datetime, timedelta, timezone

from escalate_to_a_human.fake_quest import FakeQuest, summarize
from quest_ai_runner.core.adapters import Escalation
from quest_ai_runner.runner.quest_client import QuestDecisionSink


def build(meter):  # noqa: ARG001
    def escalate(kind, args):
        if kind != "escalate":
            raise ValueError(kind)
        os.environ.pop("QAR_DECISION_DEFAULT_DEADLINE_HOURS", None)
        quest = FakeQuest(args)
        sink = QuestDecisionSink(quest, default_assignee_user_id=args.get("default_assignee"),
                                 default_deadline_hours=args.get("default_deadline_hours"))
        spec = dict(args["escalation"])
        hours = spec.pop("deadline_hours", None)
        if hours is not None:
            spec["deadline"] = datetime.now(timezone.utc) + timedelta(hours=hours)
        ids = [sink.escalate(Escalation(**spec)) for _ in range(args.get("repeat", 1))]
        return summarize(quest, ids)
    return escalate
