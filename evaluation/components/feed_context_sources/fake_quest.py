"""A fake Quest client for the context sources: the read methods the quest channels call, answering from a
synthetic ``world`` dict. It is the network edge, so it is the only thing faked."""


class FakeQuest:
    def __init__(self, world):
        self.world = world

    def list_quest_notes(self, quest_id):  # noqa: ARG002
        return [dict(n) for n in self.world.get("notes", [])]

    def list_tasks(self, goal_id=None, **kwargs):  # noqa: ARG002
        return [dict(t) for t in self.world.get("tasks", [])]

    def list_quest_goals(self, quest_id):  # noqa: ARG002
        goals = [{"id": g["id"], "name": g["name"]} for g in self.world.get("goals", [])]
        return {"period_groups": [{"goals": goals}]}

    def list_quest_goal_updates(self, quest_id, limit_per_goal=20):  # noqa: ARG002
        out = {}
        for u in self.world.get("goal_updates", []):
            out.setdefault(u["goal"], []).append({k: v for k, v in u.items() if k != "goal"})
        return out

    def list_decisions_for_quest(self, quest_id):  # noqa: ARG002
        return [dict(d) for d in self.world.get("decisions", [])]
