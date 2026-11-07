"""The ensure-one goal tool: ``create_or_update_quest_goal``.

Role: one goal per exact title on a quest, however many times it is asked for. It is the same write as ``create_quest_goal`` (an argument-compatible
sibling, not a new capability): it looks for a goal with exactly that title, creates it when absent, changes only the fields that differ when present
(including its owner and its links to the quest's strategy and metric) and reports ``created``, ``updated`` or ``unchanged``. The rules about what a
goal may link to stay in Quest, which refuses a bad link with its reason; this tool reports that reason and never reads a refusal as done.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

from quest_ai_runner.the_body.hands.quest_operations import goal_links
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_standard import (
    NO_QUEST_HINT, _ASKED_PARAM, _NEVER_CODE, _QUEST_ID_PARAM, _quest_id, _with_standard, _with_credentials)
from quest_ai_runner.the_world.tools.tools import (
    STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, quest_client_from_env, write_failed)
from quest_ai_runner.the_world.tools.tools_status import ToolStatus


def create_or_update_quest_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        title = str(args.get("title") or "").strip()
        period = str(args.get("period") or "").strip()
        if not quest_id or not title or not period:
            return ToolResult(ok=False, text="A goal needs a quest, a title and a period; nothing was written." + NO_QUEST_HINT)
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=f"Not written: the user did not ask for the goal {title!r}. Suggest it in your reply instead.")
        try:
            client = factory()
            assignee_id = None
            if args.get("assignee"):
                assignee_id, why = goal_links.resolve_assignee(client, quest_id, args["assignee"])
                if why:
                    return ToolResult(ok=False, text=f"Nothing was written: {why}")
            outcome = goal_links.ensure_goal(client, quest_id, title, period, goal_links.link_fields(args), assignee_id)
        except Exception as e:  # QuestWriteError / QuestNotConfigured: report, never raise
            return write_failed(e, "Nothing was written")
        state, writes = outcome["state"], outcome["writes"]
        if state == "unknown":
            return ToolResult(ok=False, status=ToolStatus.UNKNOWN, text=(
                f"Not written: the goals of quest {quest_id} could not be read, so it is not known whether {title!r} already exists. Try again."))
        held = [w for w in writes if getattr(w, "status", None) is ToolStatus.HELD]
        data = {"state": state, "goal_id": outcome["goal_id"], "changed": outcome["changed"]}
        if held:
            return ToolResult(ok=False, status=ToolStatus.HELD, data=data, text=(
                f"Not applied yet: the change to the goal {title!r} is waiting for a person to approve it. It is NOT done."))
        said = {"created": f"Created the {period} goal {title!r} on quest {quest_id}.",
                "updated": f"The goal {title!r} already existed; updated in place ({', '.join(outcome['changed'])}). No second goal was made.",
                "unchanged": f"The goal {title!r} already exists exactly as asked; nothing was changed."}[state]
        if outcome["copies"]:
            said += f" Note: {outcome['copies']} more goal(s) carry the same title; only the first was touched."
        return ToolResult(ok=True, status=ToolStatus.NOOP if state == "unchanged" else ToolStatus.APPLIED, data=data, text=said)

    return ToolSpec(
        name="create_or_update_quest_goal",
        handler=handler,
        description=_with_standard(
            "Make sure exactly ONE goal with this exact title exists on the quest, with this owner and these links, and say whether it was "
            "created, updated in place or already as asked. Safe to call again: a repeat never makes a second goal.", "goal"),
        when_to_use=("A recurring or keyed goal, such as a milestone's next step titled 'Pn next step: week of <Monday date>', or a named deliverable "
                     "goal, where a repeat of the request must find the goal already there. Name the owner (assignee) and link it to the "
                     "milestone with spec_solution_id (and spec_metric_id with metric_target_value) from read_spec_records or get_spec. "
                     "Keys: a milestone's next step is titled 'Pn next step: week of YYYY-MM-DD' (Pn is the code at the start of the milestone's name, the date is "
                     "the Monday of this week); a deliverable you write is titled 'Pn <short name>'. Use the same title every time so a repeat finds it. "
                     "Also how to record alternatives you wrote (two posts, three headlines): one goal each, titled by a short stable key "
                     "such as 'FB push post A', with the full text, the hypothesis and the metric they are compared by in the description, "
                     "and spec_solution_id of the solution they serve. "
                     + _NEVER_CODE),
        when_not_to_use=("A one-off goal whose title will not repeat (use create_quest_goal). A goal the user did not ask for: suggest it "
                         "instead. Renaming a goal (the title is the key, so a new title is a new goal)."),
        parameters={
            "type": "object",
            "required": ["title", "period"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "The exact title; it is the key that makes the call safe to repeat."},
                "period": {"type": "string",
                           "description": ("YYYY-MM-DD (day), YYYY_W## (week), YYYY_MM (month), YYYY_Q# (quarter) or YYYY (year). "
                                           "If the goal exists in another period it is moved to this one.")},
                "description": {"type": "string", "description": "The goal's brief; replaced when it differs."},
                "criteria": {"type": "string", "description": "How anyone can check it is done at period end."},
                "target_value": {"type": "number", "description": "Target number, when it has one."},
                "target_unit": {"type": "string", "description": "Unit of the target."},
                **goal_links.LINK_PARAMS,
                "user_asked_for_this_field": {
                    "type": "boolean",
                    "description": ("True when the user's own message asks for the work this goal records: a request to do or write something, "
                                    "or a cue to get a milestone moving (its next step is this goal), or to write alternatives (each is recorded "
                                    "here). False means it is only offered for the owner to approve.")},
            },
        },
        mutates=True,
        workflow_step=False,
        origin="standard",
        keywords=("goal", "next", "step", "milestone", "deliverable", "owner", "assign", "week", "plan", "create", "update"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=60.0,
    )


STANDARD_TOOL_BUILDERS["create_or_update_quest_goal"] = _with_credentials(create_or_update_quest_goal_spec)
