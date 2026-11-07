"""The loop planner's decision read as the route of a request: Quest's arm of the hub's Route a Request.

Role: the route the system has in practice is whatever the planner's decision says, spread over three of its fields. The planner's ``user_intent`` says whether the person asks to be told
something or asks for something to be done (``act``), its ``action`` says whether it needs the person first (``clarify``) or hands the work on (``tool``, ``deep``, ``confirm``), and its
``deep_target`` says when the deliverable is text written in the reply (``draft_in_chat``). ``report_of`` puts those three fields in the plan report the hub's ``route_from_report`` turns
into the route, so the structure of the decision is the only thing read: no word of the request and no word of the reply is looked at. One planning call over the model; nothing is
executed, no tool runs and no worker starts.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from esoi import implementation
from hubofhubs.api import ModelCalling, PlanDerived

from quest_ai_runner.the_self.loop_wiring import orchestrator_over

#: The planner's actions that ask for something to be done or decided before anything is answered.
DOING_ACTIONS = ("tool", "deep", "confirm")
DRAFT_TARGET = "draft_in_chat"      # the planner's report of a deliverable written in the reply


def report_of(plan: Any) -> Mapping[str, Any]:
    """A planner decision as the plan report of the hub's Route a Request: ``needs_more`` for a clarify, ``reply_deliverable`` for a draft in the chat, ``intent`` ``act`` when the
    planner reads the person as asking for something to be done or its action does it, else ``ask``."""
    doing = getattr(plan, "user_intent", None) == "act" or getattr(plan, "action", None) in DOING_ACTIONS
    return {"needs_more": getattr(plan, "action", None) == "clarify", "reply_deliverable": getattr(plan, "deep_target", None) == DRAFT_TARGET,
            "intent": "act" if doing else "ask", "worker": ""}


@implementation("route-a-request", "planner_derived")
class PlannerDerived(PlanDerived):
    """The route is the planner's decision read by structure: the route cannot disagree with the plan, and costs the planning call the loop makes anyway. This is the route the loop has today."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def report(self, request: str, context: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        return report_of(orchestrator_over(self.model, deep_runner=None)._plan(request, "", str((context or {}).get("view") or ""), []))
