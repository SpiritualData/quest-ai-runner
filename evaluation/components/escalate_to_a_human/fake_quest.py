"""The fake Quest client for the escalate_to_a_human component: the network edge, nothing else."""
from quest_ai_runner.runner.quest_client_support import QuestApiError


class FakeQuest:
    def __init__(self, args):
        self.open = [dict(d, decision_id=d["id"]) for d in args.get("open_decisions", [])]
        self.owner = args.get("quest_owner")
        self.team = args.get("team")
        self.create_fails = bool(args.get("create_fails"))
        self.lookup_fails = bool(args.get("lookup_fails"))
        self.created = []

    def list_open_decisions_for_quest(self, quest_id):  # noqa: ARG002
        if self.lookup_fails:
            raise QuestApiError("lookup down")
        return list(self.open)

    def owning_team_for(self, quest_id):  # noqa: ARG002
        return self.team or ""

    def get_quest(self, quest_id):  # noqa: ARG002
        return {"owner_user_ids": [self.owner]} if self.owner else {}

    def create_decision(self, summary, **kw):
        if self.create_fails:
            raise QuestApiError("backend rejected", status=500)
        did = f"dec_{len(self.created) + 1}"
        self.created.append(dict(kw, summary=summary, decision_id=did))
        self.open.append({"decision_id": did, "summary": summary})
        return {"decision_id": did}


def summarize(quest, ids):
    last = quest.created[-1] if quest.created else {}
    return {"id": ids[-1], "filed": len(quest.created), "assignee": last.get("assignee_user_id"),
            "kind": last.get("kind"), "default_on_silence": last.get("default_on_silence"),
            "review_url": last.get("review_url"), "deadline_set": last.get("deadline") is not None,
            "team_id": last.get("team_id")}
