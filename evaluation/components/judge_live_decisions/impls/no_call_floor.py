"""FLOOR, offline: the model call of each judge is dropped. The quest ranker falls back to the home quest (else none), and with no reach
verdict the planner plans as it would for a request inside the readable sources. Zero model calls."""
from quest_ai_runner.core import quest_judge


def build(meter):  # noqa: ARG001
    def dropped(prompt, tool):  # noqa: ARG001
        raise RuntimeError("the judge call is dropped")

    def judge(kind, args):
        if kind == "reach":
            return {"reach": "inside", "covered": False}
        rows = quest_judge.rank_quests(args["items"], args["quests"], dropped, home_quest_id=args.get("home"))
        return {"chosen": [chosen for chosen, ranking in rows]}
    return judge
