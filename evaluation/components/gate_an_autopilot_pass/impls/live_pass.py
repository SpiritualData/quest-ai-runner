"""CURRENT: the production ``AutopilotPass`` run for one quest against a scripted Quest client and a pinned clock. The
daily budget, cadence, "Run now", reactive, roster-day and backpressure gates are the live code; the client answers only
what a gate reads, and anything past the gates (goal reads, task creation) is recorded and answered empty, so the
outcome is exactly "skipped for this reason" or "got past every gate". Zero model calls."""
from datetime import datetime

from quest_ai_runner.runner.autopilot import AutopilotPass

QUEST = "quest_a"
TEAM = "team_a"


class ScriptedClient:
    """The slice of the Quest client the gates read; everything else is answered empty."""

    def __init__(self, args):
        self.cfg = args["cfg"]
        self.tasks = args.get("tasks") or []
        self.asks = args.get("asks")
        self.reached = []

    def list_quests(self, **kwargs):  # noqa: ARG002
        return [{"quest_id": QUEST, "outcome": "ship it", "completed": False, "owner_user_ids": []}]

    def get_quest_autopilot(self, quest_id):  # noqa: ARG002
        return {"outcome": "ship it", "autopilot": dict(self.cfg)}

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, **kwargs):  # noqa: ARG002
        return [t for t in self.tasks if (status is None or t.get("status") == status)
                and (goal_id is None or t.get("goal_id") == goal_id)]

    def list_asks(self, quest_id=None, limit=None):  # noqa: ARG002
        return self.asks

    def list_open_decisions_for_quest(self, quest_id):  # noqa: ARG002
        return []

    def __getattr__(self, name):
        def anything_else(*a, **k):  # noqa: ARG001
            self.reached.append(name)
            return []
        return anything_else


def build(meter):  # noqa: ARG001
    def gate(args):
        now = datetime.fromisoformat(args["now"].replace("Z", "+00:00"))
        client = ScriptedClient(args)
        autopilot = AutopilotPass(client, team_id=TEAM, daily_budget=args.get("budget", 3),
                                  backpressure=bool(args.get("backpressure")), now=lambda: now)
        result = autopilot.run({"text": "pass", "goal_id": QUEST})
        if result.skipped:
            return {"outcome": "skipped", "text": result.skipped[0]["reason"]}
        return {"outcome": "ran", "text": ""}
    return gate
