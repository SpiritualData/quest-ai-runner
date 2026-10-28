"""FLOOR (``hand-off-a-goal:creator_keeps_it``): no model call. A new goal stays with the person who created it (the quest owner when
an AI created it), and no goal is ever handed to an AI representative. The real ``choose_assignee`` and ``run_goal_handoff`` run with
no judge, which is what they do today when no model is configured, except that the assignee is the creator the case names."""
from hand_off_a_goal import arms


def build(meter):  # noqa: ARG001
    def hand_off(case):
        if case["kind"] == "doability":
            return arms.run_doability(case, None)
        arms.run_assignee(case, None)   # the real fallback runs; the floor's own answer is the creator
        from quest_ai_runner.runner.goal_handoff import default_owner_id
        return {"assignee": case.get("creator") or default_owner_id(case["members"]), "ai_can_do": False}
    return hand_off
