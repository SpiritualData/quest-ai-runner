"""Shared machinery of the arms: the fake Quest client and one runner per case kind over the real ``goal_handoff`` code.

Nothing here decides a verdict. A judge (or none) is handed in by the arm; the library's own functions render the prompts, parse the
answers, apply their fallbacks and stamp the hand-off."""
import common  # noqa: F401  (puts the repository on the path)
from quest_ai_runner.runner import goal_handoff

REP_OF = {"rep_id": "rep_bob", "owner_user_id": "u_bob", "is_default": True}


class FakeClient:
    """The Quest client at the boundary: lists the assignee's representative and records the hand-off it is asked for."""

    def __init__(self):
        self.stamped = []

    def list_team_reps(self, *, team_id=None):  # noqa: ARG002
        return [dict(REP_OF)]

    def set_goal_ai_handling(self, goal_id, handling, *, rep_id=None, decided_by="rep"):
        self.stamped.append((goal_id, handling, rep_id, decided_by))
        return {}


def text_judge(provider, model):
    """The ``prompt -> text`` judge the shipped code builds with ``make_judge``, over a pinned provider."""
    return lambda prompt: provider.answer([{"role": "user", "content": prompt}], model=model) or ""


def run_doability(case, judge):
    goal = case["goal"]
    client = FakeClient()
    payload = {"period_groups": [{"goals": [{"id": goal["id"], "name": goal["name"], "description": goal.get("description", ""),
                                             "assigned_to_user_id": goal["assigned_to"]}]}]}
    quest = case["quest"]
    done = goal_handoff.run_goal_handoff(client, judge, payload, team_id="team1", outcome=quest["outcome"],
                                         current_state=quest["current_state"], preferences=quest["preferences"])
    return {"ai_can_do": bool(client.stamped) and client.stamped[0][1] == "rep", "decided": bool(done)}


def run_assignee(case, judge):
    goal, quest = case["goal"], case["quest"]
    return goal_handoff.choose_assignee(judge, case["members"], outcome=quest["outcome"], title=goal["name"],
                                        description=goal.get("description", ""), current_state=quest["current_state"],
                                        preferences=quest["preferences"])


def run_case(case, judge):
    return run_doability(case, judge) if case["kind"] == "doability" else run_assignee(case, judge)
