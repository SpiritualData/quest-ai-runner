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
        period = goal_links.normalize_period(args.get("period"))
        if not quest_id or not title:
            return ToolResult(ok=False, text="A goal needs a quest and a title; nothing was written." + NO_QUEST_HINT)
        if args.get("user_asked_for_this_field") is False:       # left out means the order gate decides; an explicit false is only an offer
            return ToolResult(ok=False, text=f"Not written: the user did not ask for the goal {title!r}. Suggest it in your reply instead.")
        try:
            client = factory()
            assignee_id = None
            if args.get("assignee"):
                assignee_id, why = goal_links.resolve_assignee(client, quest_id, args["assignee"])
                if why:
                    return ToolResult(ok=False, text=f"Nothing was written: {why}")
            fields = goal_links.link_fields(args)
            adopt, link_match = str(args.get("adopt_goal_id") or "").strip() or None, bool(args.get("match_by_link"))
            links_refused = ""
            try:
                outcome = goal_links.ensure_goal(client, quest_id, title, period, fields, assignee_id, adopt, link_match)
            except Exception as refusal:  # noqa: BLE001
                if not (fields.get("spec_solution_id") or fields.get("spec_metric_id")) or not goal_links.is_link_refusal(refusal):
                    raise
                # The quest refused the link to the strategy or metric (and so the whole write): write the goal once without the links, naming the milestone in its description,
                # and say plainly that the link was not accepted.
                links_refused = str(refusal)[:300]
                bare = {k: v for k, v in fields.items() if k not in ("spec_solution_id", "spec_metric_id", "metric_target_value")}
                bare["description"] = (str(bare.get("description") or "").rstrip() + f"\nMilestone record: {fields.get('spec_solution_id') or fields.get('spec_metric_id')}").strip()
                outcome = goal_links.ensure_goal(client, quest_id, title, period, bare, assignee_id, adopt, False)
                fields = bare
        except Exception as e:  # QuestWriteError / QuestNotConfigured: report, never raise
            return write_failed(e, "Nothing was written")
        state, writes = outcome["state"], outcome["writes"]
        if state == "unknown":
            return ToolResult(ok=False, status=ToolStatus.UNKNOWN, text=(
                f"Not written: the goals of quest {quest_id} could not be read, so it is not known whether {title!r} already exists. Try again."))
        held = [w for w in writes if getattr(w, "status", None) is ToolStatus.HELD]
        if held:
            return ToolResult(ok=False, status=ToolStatus.HELD, data={"state": state, "goal_id": outcome["goal_id"], "changed": outcome["changed"]}, text=(
                f"Not applied yet: the change to the goal {title!r} is waiting for a person to approve it. It is NOT done."))
        # Only what the quest shows after the write is reported: "updated" when the read-back differs from before, links and owner as read.
        check = goal_links.verified(client, quest_id, outcome, title, fields, assignee_id)
        state = check["state"]
        shown = dict(check.get("goal") or {})
        if shown.get("assigned_to_user_id"):
            try:
                shown["assignee_name"] = next((str(m.get("name") or "") for m in (client.list_assignable_members(quest_id) or [])
                                               if str(m.get("user_id")) == str(shown["assigned_to_user_id"])), None)
            except Exception:  # noqa: BLE001  the name is a convenience for the reader; the id stays
                shown["assignee_name"] = None
        data = {"state": state, "goal_id": outcome["goal_id"], "changed": outcome["changed"], "problems": check["problems"], "links_refused": links_refused,
                "goal": {k: shown.get(k) for k in ("id", "name", "assigned_to_user_id", "assignee_name", "spec_solution_id", "spec_metric_id", "period")} if shown else None}
        said = {"created": f"Created the {period} goal {title!r} on quest {quest_id}.",
                "updated": f"The goal {title!r} already existed; updated in place ({', '.join(outcome['changed'])}). No second goal was made.",
                "unchanged": f"The goal {title!r} already exists exactly as asked; nothing was changed."}[state]
        try:
            said += " " + goal_links.read_back_text(client, quest_id, check)
        except Exception as error:  # noqa: BLE001  a read that failed is said with its cause
            said += f" Read back failed ({type(error).__name__}), so the owner and links are not confirmed."
        recorded = str(args.get("description") or "").strip()
        if recorded and state != "unchanged":
            said += f" Recorded text: {recorded[:500]}"          # the reply restates what was recorded, including each alternative's hypothesis and metric
        if links_refused:
            said += f" Quest did not accept the link to the milestone ({goal_links.refusal_reason(links_refused)}), so the goal was written without it and names the milestone in its description."
        if outcome["copies"]:
            said += f" Note: {outcome['copies']} more goal(s) carry the same title; only the first was touched."
        return ToolResult(ok=True, status=ToolStatus.NOOP if state == "unchanged" else ToolStatus.APPLIED, data=data, text=said)

    return ToolSpec(
        name="create_or_update_quest_goal",
        handler=handler,
        description=_with_standard(
            "Make sure exactly ONE goal with this exact title exists on the quest, with this owner and these links, and say whether it was "
            "created, updated in place or already as asked. Safe to call again: a repeat never makes a second goal.", "goal"),
        when_to_use=("A recurring or keyed goal, such as a milestone's next step titled '<milestone title> next step: week of <Monday date>', or a named deliverable "
                     "goal, where a repeat of the request must find the goal already there. Name the owner (assignee) and link it to the "
                     "milestone with spec_solution_id (and spec_metric_id with metric_target_value) from read_spec_records or get_spec. "
                     "Keys: a milestone's next step is titled '<milestone title> next step: week of YYYY-MM-DD' (the date is the Monday of this week in the "
                     "quest's own time zone); a deliverable you write is titled '<milestone title> <short name>'. Use the same title every time so a repeat finds it. "
                     "Also how to record alternatives you wrote (two posts, three headlines): one goal each, titled by a short stable key "
                     "such as 'FB push post A', with the full text, the hypothesis and the metric they are compared by in the description, "
                     "and spec_solution_id of the solution they serve. "
                     + _NEVER_CODE),
        when_not_to_use=("A one-off goal whose title will not repeat (use create_quest_goal). A goal the user did not ask for: suggest it "
                         "instead. Renaming a goal (the title is the key, so a new title is a new goal)."),
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "The exact title; it is the key that makes the call safe to repeat."},
                "period": {"type": "string",
                           "description": ("YYYY-MM-DD (day), YYYY_W## (week), YYYY_MM (month), YYYY_Q# (quarter) or YYYY (year), or the word week, day, month, quarter "
                                           "or year for the current one. Leave out for this week. If the goal exists in another period it is moved to this one.")},
                "description": {"type": "string", "description": "The goal's brief; replaced when it differs."},
                "criteria": {"type": "string", "description": "How anyone can check it is done at period end."},
                "target_value": {"type": "number", "description": "Target number, when it has one."},
                "target_unit": {"type": "string", "description": "Unit of the target."},
                **goal_links.LINK_PARAMS,
                "match_by_link": {"type": "boolean",
                                  "description": ("True for a goal that stands for its strategy's next step: when no goal has this exact title, the open KEYED next-step goal already linked to "
                                                  "spec_solution_id is updated in place before any goal is created. A person's own goal linked to it is never touched.")},
                "adopt_goal_id": {"type": "string",
                                  "description": ("The id of an existing KEYED next-step goal (its title ends 'next step: week of YYYY-MM-DD') that is the same work under an older title. "
                                                  "When no goal has this exact title, that goal is renamed to this title and updated in place instead of a second goal being created. "
                                                  "Any other goal belongs to a person and is never renamed, re-described or moved: the id is ignored.")},
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
