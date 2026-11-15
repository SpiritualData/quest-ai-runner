"""BROKEN: the naive gate a first draft would write. It counts EVERY run against the team's daily budget (scheduled ones, Run now and extras alike),
so a quest with more autopilots than the budget starves the later ones in list order, and it holds a run for any open run on the quest rather than its
own autopilot's. A test proves it fails the gate cases. Zero model calls."""


def build(meter):  # noqa: ARG001
    def gate(args):
        today = args["now"][:10]
        open_states = {"queued", "in_progress", "needs_you", "suggested"}
        made = [t for t in args.get("tasks") or [] if t.get("task_kind") == "autopilot_run" and str(t.get("created_at", ""))[:10] == today]
        if len(made) >= args.get("budget", 3):
            return {"outcome": "skipped", "text": "budget used up"}
        if args.get("backpressure") and any(t.get("status") in open_states for t in args.get("tasks") or []):
            return {"outcome": "skipped", "text": "backpressure"}
        return {"outcome": "ran", "text": ""}
    return gate
