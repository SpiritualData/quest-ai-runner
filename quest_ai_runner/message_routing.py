"""``route-a-message``: where a message goes. The orchestrator's planner reads the message and decides (answer, a tool and its
arguments, deep work, a confirm or a clarify); ``route`` returns that decision as plain data and executes nothing.

The interface is declared here because two areas use it: ``take-in-the-world`` routes every request, and ``core-ml-tasks`` reads the
route as a classification label.
"""
from __future__ import annotations

import typing
from typing import Any, Dict, Mapping

from esoi import implementation, solution
from hubofhubs.seats import ModelCalling

from .loop_wiring import orchestrator_over


def route_of(plan: Any) -> Dict[str, Any]:
    """A planner decision as the route: ``route`` is ``tool``, ``deep``, ``confirm``, ``clarify`` or ``answer``; a tool route names the tool."""
    calls = [{"name": c["name"], "args": c.get("args") or {}} for c in (plan.tool_calls or [])]
    if plan.action == "tool" and calls:
        return {"route": "tool", "tool": calls[0]["name"], "calls": calls, "raw": plan.action}
    if plan.action in ("deep", "confirm", "clarify"):
        return {"route": plan.action, "tool": None, "calls": [], "raw": plan.action}
    return {"route": "answer", "tool": None, "calls": [], "raw": plan.action}


def route_message(model: Any, message: str) -> Dict[str, Any]:
    """The route the loop's planner decides for ``message`` over ``model``: one planning call, nothing executed."""
    return route_of(orchestrator_over(model, deep_runner=None)._plan(message, "", "", []))


@solution("route-a-message", kind="event", entry="route", example="Save a note that the launch moves to Friday.")
@typing.runtime_checkable
class MessageRouting(typing.Protocol):
    """Decides where one message goes, as data: ``{"route", "tool", "calls", "raw"}``."""

    def route(self, message: str) -> Mapping[str, Any]: ...


@implementation("route-a-message", "planner_route")
class PlannerRoute:
    """The loop's own planner makes one planning call over the model and its decision is the route. No tool runs and no worker starts."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def route(self, message: str) -> Mapping[str, Any]:
        return route_message(self.model, message)
