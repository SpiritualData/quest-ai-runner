"""The tools the milestone flow runs on, built over a host's own Quest client.

Role: a flow step of ESOI Application. The milestone flow (``milestone_cue``) finds the milestone a short cue concerns and records its keyed next step through four tools of
the turn's registry: ``read_spec_records``, ``list_quest_goals``, ``create_or_update_quest_goal`` and, when the host can read a metric's source, ``read_metric_source``. A lane
gets them from its environment (``QAR_BASE_URL`` and a key). A host that runs the brain inside its own service has no such environment: it hands this function a client factory
that answers the Quest client's calls in process, and, for the metric sources, a reader. Nothing else about the tools differs, so the guards that keep a person's goal untouched
and the receipts that say what was read back are the lane's own.

The client the factory returns needs: ``get_spec``, ``get_quest_autopilot`` (optional), ``list_quest_goals``, ``list_assignable_members``, ``create_goal``, ``update_goal`` and
``assign_goal``, with the shapes ``QuestClient`` documents (a write returns a ``WriteResult`` or raises a ``QuestWriteError``), and ``create_spec_record`` / ``edit_spec_record``
for the intermediate metric a progress turn may add through ``apply_spec_edit``.

``keep_milestone_deliverable`` keeps the deliverable a progress turn made (a script, an analysis, listing copy) on the milestone's keyed goal, updated in place on a
repeat, with the increment's measured before and after in its result so a reader of the turn can trace them. It never assigns or dates anything: it is the work.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_intents import read_spec_records_spec
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import list_quest_goals_spec
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import metric_reader_spec, recorded_reader_spec
from quest_ai_runner.the_body.hands.quest_operations import goal_links
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_intents import apply_spec_edit_spec
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolResult, ToolSpec, write_failed
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

#: The fields of an increment the keep tool carries back, as measured by code (never the writer's say-so).
INCREMENT_FIELDS = ("kind", "measure_id", "measure_name", "before", "after", "total", "unit", "method", "as_of", "changed", "simulated")


def keep_milestone_deliverable_spec(client_factory: Callable[[], Any]) -> ToolSpec:
    """``keep_milestone_deliverable``: one goal per exact ``title`` (the milestone's title then a short stable name), its description the marker and the
    deliverable's ``body``, linked to ``solution_id``; created, updated in place, or already as asked. The result's data carries the goal as read back, the
    body it held before (so the before can be measured on the kept version) and the ``increment`` the flow measured."""

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = str(args.get("quest_id") or ctx.quest_id or "").strip()
        title, body = str(args.get("title") or "").strip(), str(args.get("body") or "").strip()
        if not quest_id or not title or not body:
            return ToolResult(ok=False, text="Nothing was kept: a deliverable needs a quest, a title and a body.")
        fields = {"description": body}
        if args.get("solution_id"):
            fields["spec_solution_id"] = str(args["solution_id"])
        try:
            client = client_factory()
            outcome = goal_links.ensure_goal(client, quest_id, title, goal_links.normalize_period(""), fields, None)
        except Exception as error:  # noqa: BLE001  a refused or failed write is reported, never read as kept
            return write_failed(error, "Nothing was kept")
        state = outcome["state"]
        if state == "unknown":
            return ToolResult(ok=False, status=ToolStatus.UNKNOWN, text=f"Not kept: the goals of quest {quest_id} could not be read, so it is not known whether {title!r} exists.")
        if state == "left_alone":
            return ToolResult(ok=False, status=ToolStatus.REFUSED, text=f"Not kept: a goal titled {title!r} belongs to a person and was left as it is.")
        if any(getattr(w, "status", None) is ToolStatus.HELD for w in outcome["writes"]):
            return ToolResult(ok=False, status=ToolStatus.HELD, text=f"Not kept yet: the change to {title!r} waits for a person to approve it.")
        before = outcome.get("before") or {}
        increment = {k: v for k, v in dict(args.get("increment") or {}).items() if k in INCREMENT_FIELDS}
        data = {"state": state, "title": title, "goal": {"id": outcome.get("goal_id"), "name": title, "spec_solution_id": fields.get("spec_solution_id")},
                "kept_before": str(before.get("description") or before.get("text") or ""), "increment": increment or None}
        said = {"created": f"Kept the deliverable as the new goal {title!r}.", "updated": f"Updated the deliverable kept in the goal {title!r} in place (no second goal).",
                "unchanged": f"The goal {title!r} already holds this deliverable; nothing was changed."}[state]
        return ToolResult(ok=True, status=ToolStatus.NOOP if state == "unchanged" else ToolStatus.APPLIED, data=data, text=said)

    return ToolSpec(
        name="keep_milestone_deliverable", handler=handler,
        description="Keep the deliverable a progress turn made on the milestone's keyed goal (one goal per exact title, updated in place), with its measured increment.",
        when_to_use="Run by the milestone flow's code after it made and measured a deliverable.",
        when_not_to_use="Anything else: an owner, a due date or a reminder is never a deliverable.",
        parameters={"type": "object", "required": ["title", "body"], "properties": {
            "quest_id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "solution_id": {"type": "string"},
            "increment": {"type": "object"}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True, flow_owned=True, context_args={"quest_id": "quest_id"})


def milestone_flow_tools(client_factory: Callable[[], Any], metric_reader: Optional[Callable[[str], Mapping[str, Any]]] = None,
                         metric_source_names: Optional[Sequence[str]] = None,
                         recorded_reader: Optional[Callable[[str, str], Mapping[str, Any]]] = None) -> List[ToolSpec]:
    """The milestone flow's tools over ``client_factory() -> client``. ``metric_reader(name) -> {"value", "as_of", "source", "detail"}`` (raising ``MetricSourceError`` in plain words
    for a source it cannot read) adds ``read_metric_source``; leave it out when the host reads no source. ``recorded_reader(quest_id, metric_id) -> reading`` adds
    ``read_recorded_metric_value``, the latest value recorded for the metric record in the host's own store, which the flow asks before it runs the record's named source."""
    # The goal write and the metric reads belong to the flow's code step, which the milestone cue starts: the planner of a turn the cue did not claim is never shown them
    # (``flow_owned``), so an ordinary request ("write an Instagram post", "remind me every Monday") is not turned into a goal. The two reads below stay offered.
    specs = [read_spec_records_spec({}, client_factory), list_quest_goals_spec({}, client_factory),
             dataclasses.replace(create_or_update_quest_goal_spec({}, client_factory), on_request_only=True, flow_owned=True)]
    if metric_reader is not None:
        specs.append(dataclasses.replace(metric_reader_spec(metric_reader, metric_source_names), on_request_only=True, flow_owned=True))
    if recorded_reader is not None:
        specs.append(dataclasses.replace(recorded_reader_spec(recorded_reader), on_request_only=True, flow_owned=True))
    # A progress turn keeps its deliverable, and may add one intermediate metric record, through these two; both belong to the flow's code step.
    specs.append(keep_milestone_deliverable_spec(client_factory))
    specs.append(dataclasses.replace(apply_spec_edit_spec({}, client_factory), on_request_only=True, flow_owned=True))
    return specs
