"""CURRENT: the production ``AutopilotPass`` planning one ``autopilot_run`` task against a scripted Quest client and a pinned clock. The extra-run
allowance, backpressure, the plan's cap, the quest's pause and the enabled switch are the live code; the client answers only what a gate reads, and
everything past the gates (goals, notes) is answered empty. Zero model calls."""
from datetime import datetime

from quest_ai_runner.intelligence.proactivity.autopilot import AutopilotPass

QUEST = "quest_a"
TEAM = "team_a"


class ScriptedClient:
    """The slice of the Quest client the gates read; everything else is answered empty."""

    def __init__(self, args):
        self.args = args

    def get_quest_autopilot(self, quest_id):  # noqa: ARG002
        return {"outcome": "ship it", "autopilot": {"autopilots": self.args["autopilots"], **(self.args.get("quest") or {})}}

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, **kwargs):  # noqa: ARG002
        return [t for t in self.args.get("tasks") or [] if (status is None or t.get("status") == status) and (goal_id is None or t.get("goal_id") == goal_id)]

    def list_open_decisions_for_quest(self, quest_id):  # noqa: ARG002
        return []

    def list_asks(self, quest_id=None, limit=None):  # noqa: ARG002
        return []

    def __getattr__(self, name):
        def anything_else(*a, **k):  # noqa: ARG001
            return []
        return anything_else


def build(meter):  # noqa: ARG001
    def gate(args):
        now = datetime.fromisoformat(args["now"].replace("Z", "+00:00"))
        autopilot = AutopilotPass(ScriptedClient(args), team_id=TEAM, daily_budget=args.get("budget", 3),
                                  backpressure=bool(args.get("backpressure")), now=lambda: now)
        plan = autopilot.run({"text": "run", "goal_id": QUEST, "task_kind": "autopilot_run", **args["task"]})
        if plan.status == "skipped" and "no goals relevant" not in plan.reason:
            return {"outcome": "skipped", "text": plan.reason}
        return {"outcome": "ran", "text": ""}
    return gate
