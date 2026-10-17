"""BASELINE: file a decision for every escalation, straight away. Uses only the fields given (no owner or
default fallback, so an unaddressed ask goes out), drops the review link and deadline, never checks for
an already open ask, never routes to the quest's team, and lets a backend error escape. The floor the
sink must beat. Zero model calls."""
from escalate_to_a_human.fake_quest import FakeQuest, summarize


def build(meter):  # noqa: ARG001
    def escalate(kind, args):
        if kind != "escalate":
            raise ValueError(kind)
        quest = FakeQuest(args)
        spec = args["escalation"]
        ids = []
        for _ in range(args.get("repeat", 1)):
            res = quest.create_decision(spec["summary"], kind=spec.get("kind", "approve"), quest_id=spec.get("quest_id"),
                                        assignee_user_id=spec.get("assignee") or "",
                                        default_on_silence=spec.get("default_on_silence", "hold"))
            ids.append(res["decision_id"])
        return summarize(quest, ids)
    return escalate
