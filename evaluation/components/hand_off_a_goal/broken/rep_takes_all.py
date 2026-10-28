"""BASELINE: the shortcut a first draft would write. Every goal is judged AI-doable and handed to the representative, and every new
goal goes to the last person listed, with no reading of the goal. Zero model calls; the cases must fail it."""
from hand_off_a_goal import arms


def build(meter):  # noqa: ARG001
    def hand_off(case):
        if case["kind"] == "doability":
            return arms.run_doability(case, lambda prompt: '{"ai_can_do": true, "reason": "an AI can do anything"}')
        last = case["members"][-1]["user_id"]
        return arms.run_assignee(case, lambda prompt: '{"assignee": "%s", "ai_can_do": true, "reason": "an AI can do anything"}' % last)
    return hand_off
