"""Runs one end-to-end case through the real orchestrator and folds the outcome into the record ``interface.py`` names.

The arms differ only in the model at the boundary and build their own orchestrator (a planner that replays the case's ideal
decisions, or the way production builds it on the claude CLI backend); ``run_terminal`` runs the real ``send`` command. A fresh orchestrator, scratch corpus and world per case, so no case sees
another's turns."""
from assist_a_terminal_session import arms as terminal_arms
from end_to_end.world import World
from scripted_brain import ScriptedProvider

PRIORITY = ("confirm", "clarify", "deep", "queue", "act", "web", "answer")


class IdealPlanner(ScriptedProvider):
    """The scripted planner of a case: its ideal decisions in order, then its ideal reply."""

    def __init__(self, decisions, reply):
        super().__init__(decisions)
        self.reply = reply

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return self.reply


def fold(case, world, result):
    """The record of a finished turn: reply, writes, reads, web queries and the one route it took."""
    kind = getattr(result, "kind", "answer")
    exit_reason = getattr(result, "exit_reason", "") or ""
    tags = set()
    if kind == "confirm" or exit_reason in ("confirm", "overseer_escalated_human", "parked"):
        tags.add("confirm")
    if exit_reason == "clarify":
        tags.add("clarify")
    if kind == "deep":
        tags.add("deep")
    if any(w["tool"] == "create_task" for w in world.writes):
        tags.add("queue")
    if any(w["tool"] != "create_task" for w in world.writes):
        tags.add("act")
    if world.web.queries:
        tags.add("web")
    text = getattr(result, "text", None) or getattr(result, "question", None) or ""
    route = next((t for t in PRIORITY if t in tags), "answer")
    return dict(reply=str(text), writes=list(world.writes), reads=[r["tool"] for r in world.reads], web=list(world.web.queries),
                route=route, exit_reason=exit_reason, kind=kind)


def new_world(case):
    spec = case.get("world") or {}
    return World(pinned=spec.get("pinned"), web=case.get("web"))


def run_with(case, world, orchestrator):
    """Send the case's message through ``orchestrator`` (built by the arm over ``world``) and fold the outcome."""
    result = orchestrator.run(case["message"], quest_id=world.pinned, context_view=world.context_view())
    return fold(case, world, result)


def run_terminal(case, provider_factory):
    """The real ``send`` command; ``provider_factory`` is None for the no-model floor."""
    out = terminal_arms.run_ack({"request": case["message"], "create_task": "ok"}, provider_factory)
    return dict(reply=out["stdout"], writes=[], reads=[], web=[], route="terminal", terminal=out)
