"""The standard Quest tools every QAR user with Quest credentials gets: the quest mailer and
email-draft filer, one tool per quest field, and the tools that add a measurable outcome, goal,
strategy or habit. Each is a ``tools.ToolSpec`` built by a ``*_spec`` function and registered
into ``tools.STANDARD_TOOL_BUILDERS`` when this module is imported.

``core.tools`` re-exports every name here, so existing imports keep working. The registry, the
Quest client factory and the loader are in ``tools``; asks, progress, notes and goals reads are in
``tools_quest_ops`` and experiments in ``tools_quest_experiments``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .tools import (
    STANDARD_TOOL_BUILDERS,
    ToolContext,
    ToolResult,
    ToolSpec,
    quest_client_from_env,
    quest_credentials_present,
)


def send_quest_email_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """The standard quest mailer: ``POST /api/quests/{id}/email`` through QuestClient."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        client = factory()
        result = client.send_quest_email(
            args["quest_id"], subject=args["subject"], body=args["body"],
            rep_id=args.get("rep_id"), task_id=args.get("task_id") or ctx.task_id,
            recipients=args.get("to") or None)
        who = ", ".join(args.get("to") or []) or "the quest's configured recipients"
        persona = (result or {}).get("persona") or "Quest AI"
        return ToolResult(ok=True, data=result,
                          text=f"Sent \"{args['subject']}\" to {who} as {persona} "
                               f"(quest {args['quest_id']}).")

    return ToolSpec(
        name="send_quest_email",
        handler=handler,
        description=("Send an email now through Quest's mailer for a quest: it carries the "
                     "quest's Reply-To (answers come back as quest notes), unsubscribe handling, "
                     "a record on the quest, and signs as the persona."),
        when_to_use=("The user asks to email or notify themselves, their team, or the quest's own "
                     "people, and the message can go out immediately with no human review."),
        when_not_to_use=("The message needs a person's review before sending (for example mail to "
                         "people outside the team, when a review tool is listed), or no quest is "
                         "known. Never use it to work around email being disabled on a quest."),
        parameters={
            "type": "object",
            "required": ["quest_id", "subject", "body"],
            "properties": {
                "quest_id": {"type": "string",
                             "description": "Quest the mail belongs to (defaults to this chat's quest)."},
                "subject": {"type": "string", "description": "Subject line."},
                "body": {"type": "string",
                         "description": "Full message body, plain text or markdown, final wording."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": ("One-off recipient override for this send only; omit to "
                                       "use the quest's own recipients.")},
                "rep_id": {"type": "string",
                           "description": "AI rep id whose name signs the mail (optional)."},
            },
        },
        mutates=True,
        workflow_hidden_params=("to", "rep_id"),  # a step mails only the quest's own people, as the step's persona; outside mail is file_email_draft
        origin="standard",
        keywords=("email", "mail", "send", "notify", "message", "inbox"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def file_email_draft_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """File an email draft into the user's Quest Approval Queue: ``POST /api/email-drafts``.

    Nothing is sent. A person reviews the draft in the Quest app and approving it is what sends
    it, through Quest's single send path. One draft per recipient: a draft is one message to one
    person, so several outside readers means several calls.
    """
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        recipient = str(args.get("to") or "").strip()
        if not recipient:
            return ToolResult(ok=False, text="No recipient was given, so no draft was filed.")
        client = factory()
        result = client.create_email_draft(
            recipient=recipient, subject=args["subject"], body=args["body"],
            cc=args.get("cc") or None, task_id=args.get("task_id") or ctx.task_id)
        draft_id = (result or {}).get("id")
        if not draft_id:
            return ToolResult(ok=False, data=result,
                              text="The draft was not filed: Quest did not return a draft id.")
        return ToolResult(ok=True, data=result,
                          text=(f"Filed a draft \"{args['subject']}\" to {recipient} in the "
                                f"Approval Queue (draft {draft_id}). NOT sent: it goes out only "
                                f"after the person approves it in the Quest app."))

    return ToolSpec(
        name="file_email_draft",
        handler=handler,
        description=("File an email draft into the user's Approval Queue in the Quest app. "
                     "Nothing is sent: the person reviews and edits it there, and approving it "
                     "sends it through Quest's mailer with unsubscribe handling."),
        when_to_use=("The user asks for an email to someone outside their own team (a donor, "
                     "partner, applicant, lead, any one-off contact), or asks for a draft to "
                     "review, or the message is one a person should read before it goes out."),
        when_not_to_use=("An update to the quest's own people that can go out immediately: use "
                         "send_quest_email. Never to bulk-mail a list. Never tell the user the "
                         "email was sent: it is a draft awaiting their approval."),
        parameters={
            "type": "object",
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {"type": "string", "description": "The one recipient email address."},
                "subject": {"type": "string", "description": "Subject line."},
                "body": {"type": "string",
                         "description": "Full message body, plain text, ready to send. No em dashes."},
                "cc": {"type": "array", "items": {"type": "string"},
                       "description": ("Extra CC addresses. The account's standing Always CC is "
                                       "added automatically; do not add it yourself.")},
            },
        },
        mutates=True,
        origin="standard",
        keywords=("email", "draft", "review", "approval", "queue", "outreach", "external"),
        timeout_seconds=45.0,
    )


# ---------------------------------------------------------------------------------------------
# Quest data tools: ONE tool per quest field, plus goals, strategies and habits
# ---------------------------------------------------------------------------------------------
#
# WHY ONE TOOL PER FIELD (2026-10-11). The single ``update_quest_fields`` tool that came before
# took any of seven fields in one ``fields`` object, so its description could only say WHICH fields
# exist, never how to write each one, and AI-written vision statements, current states and
# measurable outcomes read as generic. Each tool below writes exactly one field (or adds one goal,
# strategy or habit) and its description carries that field's own writing standard from
# ``core.quest_field_instructions`` (built from the vendored backend YAML), so the model reads the
# standard at the moment it chooses the write. They remain the ONLY way an AI changes quest data: never generated code, a file edit or a
# coding agent (Joshua, 2026-10-01: "AI generated code should never be used for quest field
# updates, qar has a specific tool for that").
#
# GOVERNANCE IS THE BACKEND'S. Every field write carries ``actor="ai"`` and the structured
# ``userRequested`` verdict, and Quest's AI field-write gate decides: a write the person did not ask
# for, or any write on a quest whose autopilot is off, becomes an ask for the owner instead of an
# edit, which these tools report as NOT applied. Goals and habits are new items rather than the
# person's own words, so the backend does not gate them; these tools refuse to create one the
# person did not ask for and say to suggest it instead.

#: Field name on the quest -> the tool that writes it. Tool names say the field in plain words.
QUEST_FIELD_TOOLS: Dict[str, str] = {
    "outcome": "update_quest_outcome",
    "name": "update_quest_name",
    "purpose": "update_quest_purpose",
    "current_state": "update_quest_current_state",
    "preferences": "update_quest_preferences",
    "acceptance_criteria": "update_quest_acceptance_criteria",
    "scope": "update_quest_scope",
    "quest_goal": "update_quest_goal_statement",
    "quest_completion_criteria": "update_quest_completion_criteria",
}

#: Fields the backend's AI field-write gate does not cover, so the tool itself holds the line: a
#: write the person did not ask for is not sent at all.
UNGATED_QUEST_FIELDS = frozenset({"scope"})

_NEVER_CODE = ("This tool is the ONLY way this changes: never write code, edit a file, or run a "
               "coding agent to do it.")
_SUGGEST_INSTEAD = ("Do not use it off the back of small work inside the quest (a task, a "
                    "progress note, one new goal): suggest the change in your reply instead.")

_ASKED_PARAM = {
    "type": "boolean",
    "description": ("True ONLY when the user's own message explicitly asked for THIS change. It "
                    "is a verdict you already hold from what they said, never something to read "
                    "out of your own wording. False means it is offered for the owner to approve "
                    "(or, for an item, not created) instead of applied."),
}
_QUEST_ID_PARAM = {"type": "string", "description": "Quest to change (defaults to this chat's quest)."}


def _with_standard(description: str, key: str, period: Optional[str] = None) -> str:
    from .quest_field_instructions import (UNIVERSAL_RULES, field_instructions_text,
                                           goal_instructions_text, what_a_quest_is)
    standard = goal_instructions_text(period) if key == "goal" else field_instructions_text(key)
    always = "\n".join(f"- {r}" for r in UNIVERSAL_RULES)
    return (f"{description}\n\nWHAT A QUEST IS: {what_a_quest_is('short')}\n\n"
            f"HOW TO WRITE IT:\n{standard}\nAlways:\n{always}")


def _not_applied_text(what: str, quest_id: str, result: Mapping[str, Any]) -> str:
    reason = result.get("reason") or result.get("message") or "it needs the owner's approval"
    return (f"NOT applied: {what} on quest {quest_id} was not changed ({reason}). It is now an ask "
            f"for the owner to approve, so the quest reads exactly as it did.")


def _quest_id(args: Mapping[str, Any], ctx: ToolContext) -> Optional[str]:
    return str(args.get("quest_id") or ctx.quest_id or "") or None


def quest_field_tool_spec(field_name: str, env: Mapping[str, str],
                          client_factory=None) -> ToolSpec:
    """The tool that writes ONE quest field through ``PATCH /api/quests/{id}/field``."""
    from .quest_field_instructions import QUEST_FIELD_INSTRUCTIONS
    factory = client_factory or (lambda: quest_client_from_env(env))
    spec_text = QUEST_FIELD_INSTRUCTIONS[field_name]

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there is nothing to update.")
        value = args.get("value")
        if not isinstance(value, str) or not value.strip():
            return ToolResult(ok=False, text=f"No new {spec_text.label} was given, so nothing was "
                                             f"written.")
        requested = bool(args.get("user_asked_for_this_field"))
        if field_name in UNGATED_QUEST_FIELDS and not requested:
            return ToolResult(ok=False, text=(f"Not written: the user did not ask to change the "
                                              f"{spec_text.label}. Suggest it in your reply instead."))
        result = factory().edit_quest_field(quest_id, {field_name: value.strip()},
                                            actor="ai", user_requested=requested)
        if not result:
            return ToolResult(ok=False, data=result,
                              text=f"Nothing was written: the update of {field_name} on quest "
                                   f"{quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text(field_name, quest_id, result))
        return ToolResult(ok=True, data=result, text=f"Updated {field_name} on quest {quest_id}.")

    return ToolSpec(
        name=QUEST_FIELD_TOOLS[field_name],
        handler=handler,
        description=_with_standard(
            f"Write a quest's {spec_text.label} through Quest's governed field-update route.",
            field_name),
        when_to_use=(f"The user asked to set or change their quest's {spec_text.label.lower()}. "
                     + _NEVER_CODE),
        when_not_to_use=("The user did not ask for THIS field to change in this message. "
                         + _SUGGEST_INSTEAD + " Not for code or file changes of any kind."),
        parameters={
            "type": "object",
            "required": ["value"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "value": {"type": "string",
                          "description": (f"The FULL new {spec_text.label}, written to the "
                                          f"standard in this tool's description. Merge new "
                                          f"information into the current value rather than "
                                          f"replacing it with a fragment.")},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_hidden_params=("user_asked_for_this_field",),  # a step cannot assert the user asked; Quest's field-write gate decides
        origin="standard",
        keywords=("quest", "field", "update", "change", "set", field_name.replace("_", " "),
                  *spec_text.label.lower().replace("(", " ").replace(")", " ").split()),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def add_measurable_outcome_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Add ONE item to a quest's measurable outcomes, keeping every existing item untouched.

    The field is a whole list on the API, so the tool reads the current list and writes it back
    with the new item appended (the backend keeps every progress field and id the request carries
    or leaves out). The write passes the same AI field-write gate as any other quest field.
    """
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there is nothing to update.")
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, text="No measurable outcome text was given, so nothing "
                                             "was written.")
        client = factory()
        state = client.get_my_quest(quest_id)
        quest = (state.get("state") if isinstance(state, dict) else None) or state or {}
        if not quest:
            return ToolResult(ok=False, text=f"Nothing was written: quest {quest_id} could not be "
                                             f"read, so its current outcomes are unknown.")
        current = [o for o in (quest.get("measurable_outcomes") or quest.get("measurableOutcomes")
                               or []) if isinstance(o, dict)]
        if any(str(o.get("text") or "").strip().lower() == text.lower() for o in current):
            return ToolResult(ok=True, text=f"Not added: quest {quest_id} already has the "
                                            f"measurable outcome {text!r}.")
        item: Dict[str, Any] = {"text": text, "completed": False}
        for key in ("target_value", "target_unit", "start_value"):
            if args.get(key) not in (None, ""):
                item[key] = args[key]
        result = client.edit_quest_field(quest_id, {"measurable_outcomes": current + [item]},
                                         actor="ai",
                                         user_requested=bool(args.get("user_asked_for_this_field")))
        if not result:
            return ToolResult(ok=False, text=f"Nothing was written: adding the measurable outcome "
                                             f"to quest {quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text("measurable_outcomes", quest_id, result))
        return ToolResult(ok=True, data=result,
                          text=f"Added the measurable outcome {text!r} to quest {quest_id}.")

    return ToolSpec(
        name="add_quest_measurable_outcome",
        direct=True,
        handler=handler,
        description=_with_standard(
            "Add one measurable outcome to a quest's checklist, leaving the existing items as "
            "they are.", "measurable_outcomes"),
        when_to_use=("The user asked to add a measurable outcome (a success metric) to their "
                     "quest, or the quest is being specified for the first time. " + _NEVER_CODE),
        when_not_to_use=("A new goal or task: give THAT goal its own criteria instead. Rewording "
                         "or deleting existing outcomes. " + _SUGGEST_INSTEAD),
        parameters={
            "type": "object",
            "required": ["text"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "text": {"type": "string",
                         "description": "The outcome, one countable metric with its target."},
                "target_value": {"type": "number", "description": "The target number, e.g. 1000."},
                "target_unit": {"type": "string", "description": "Its unit, e.g. 'subscribers'."},
                "start_value": {"type": "number", "description": "Baseline, when known."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_hidden_params=("user_asked_for_this_field",),  # a step cannot assert the user asked; Quest's field-write gate decides
        origin="standard",
        keywords=("quest", "measurable", "outcome", "metric", "success", "target", "kpi", "add"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def create_quest_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Create one Goal on a quest's plan, scoped to a day, week, month, quarter or year."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        title = str(args.get("title") or "").strip()
        period = str(args.get("period") or "").strip()
        if not quest_id or not title or not period:
            return ToolResult(ok=False, text="A goal needs a quest, a title and a period; nothing "
                                             "was created.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not created: the user did not ask for the goal "
                                              f"{title!r}. Suggest it in your reply instead."))
        kwargs = {k: args[k] for k in ("description", "criteria", "target_value", "target_unit",
                                       "parent_goal_id") if args.get(k) not in (None, "")}
        try:
            goal = factory().create_goal(title, period=period, quest_id=quest_id, **kwargs)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        if not goal:
            return ToolResult(ok=False, text=f"Nothing was created: the goal {title!r} was refused.")
        return ToolResult(ok=True, data=goal,
                          text=f"Created the {period} goal {title!r} on quest {quest_id}.")

    return ToolSpec(
        name="create_quest_goal",
        direct=True,
        handler=handler,
        description=_with_standard(
            "Create one goal on a quest's plan for one period (day, week, month, quarter or "
            "year). Follow the nuance for the goal's period.", "goal"),
        when_to_use=("The user asked to add a goal (or a day's, week's, month's, quarter's or "
                     "year's goal) to their quest. " + _NEVER_CODE),
        when_not_to_use=("Background AI work for later (that is a task, not a goal). A goal the "
                         "user did not ask for: suggest it instead. Changing the quest's own "
                         "fields or measurable outcomes."),
        parameters={
            "type": "object",
            "required": ["title", "period"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "Verb-first goal title."},
                "period": {"type": "string",
                           "description": ("The goal's period: YYYY-MM-DD (day), YYYY_W## (week), "
                                           "YYYY_MM (month), YYYY_Q# (quarter) or YYYY (year).")},
                "description": {"type": "string", "description": "Optional detail."},
                "criteria": {"type": "string",
                             "description": "How anyone can check it is done at period end."},
                "target_value": {"type": "number", "description": "Target number, when it has one."},
                "target_unit": {"type": "string", "description": "Unit of the target."},
                "parent_goal_id": {"type": "string",
                                   "description": "The longer-period goal this one serves."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_step=False,  # needs the user's own say-so (user_asked_for_this_field); a scheduled step cannot assert it
        origin="standard",
        keywords=("goal", "plan", "day", "week", "month", "quarter", "year", "add", "create",
                  "today", "this week"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def add_quest_strategy_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Add one strategy to a quest through ``POST /api/quests/{id}/strategies/add`` as the AI."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        title = str(args.get("title") or "").strip()
        if not quest_id or not title:
            return ToolResult(ok=False, text="A strategy needs a quest and a title; nothing was "
                                             "added.")
        raw_ids = args.get("problem_ids")
        problem_ids = [str(i).strip() for i in raw_ids if str(i).strip()] \
            if isinstance(raw_ids, (list, tuple)) else []
        result = factory().add_quest_strategy(
            quest_id, title, str(args.get("approach") or "").strip(),
            actor="ai", user_requested=bool(args.get("user_asked_for_this_field")),
            problem_ids=problem_ids or None, purpose=str(args.get("purpose") or "").strip(),
            description=str(args.get("description") or "").strip())
        if not result:
            return ToolResult(ok=False, text=f"Nothing was added: the strategy {title!r} on quest "
                                             f"{quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text(f"the strategy {title!r}", quest_id, result))
        return ToolResult(ok=True, data=result,
                          text=f"Added the strategy {title!r} to quest {quest_id}.")

    return ToolSpec(
        name="add_quest_strategy",
        direct=True,
        handler=handler,
        description=_with_standard("Add one strategy (a candidate solution) to a quest.", "strategy"),
        when_to_use="The user asked to add a strategy or approach to their quest. " + _NEVER_CODE,
        when_not_to_use=("A single goal or task (use create_quest_goal). A strategy the user did "
                         "not ask for: suggest it instead."),
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "3 to 8 words naming the approach."},
                "approach": {"type": "string",
                             "description": "The strategy statement: one sentence on how it solves "
                                            "problems, true of every implementation. Starts with a "
                                            "verb and names no actor."},
                "purpose": {"type": "string",
                            "description": "Optional. The niche of utility it serves, one sentence "
                                           "starting with a verb."},
                "description": {"type": "string",
                                "description": "Optional. Its unique value proposition, apart from "
                                               "the approach."},
                "problem_ids": {"type": "array", "items": {"type": "string"},
                                "description": "Ids of the quest's Problems this strategy "
                                               "addresses. A strategy with none addresses nothing, "
                                               "so pass them when the Problems are known."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_step=False,  # needs the user's own say-so (user_asked_for_this_field); a scheduled step cannot assert it
        origin="standard",
        keywords=("strategy", "approach", "method", "quest", "add"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def create_quest_habit_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Create one habit under a quest strategy through ``POST /api/planning/habits``."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        name = str(args.get("name") or "").strip()
        if not quest_id or not name:
            return ToolResult(ok=False, text="A habit needs a quest and a name; nothing was "
                                             "created.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not created: the user did not ask for the habit "
                                              f"{name!r}. Suggest it in your reply instead."))
        client = factory()
        strategy_id = str(args.get("strategy_id") or "").strip()
        if not strategy_id:
            state = client.get_my_quest(quest_id)
            quest = (state.get("state") if isinstance(state, dict) else None) or state or {}
            strategies = [s for s in (quest.get("strategies") or []) if isinstance(s, dict)]
            accepted = [s for s in strategies if s.get("accepted") is True] or strategies
            strategy_id = str((accepted[0] if accepted else {}).get("id") or "")
        if not strategy_id:
            return ToolResult(ok=False, text=(f"Nothing was created: quest {quest_id} has no "
                                              f"strategy to put the habit under. Add a strategy "
                                              f"first (add_quest_strategy)."))
        fields = {k: args[k] for k in ("description", "days", "time", "duration", "start_date",
                                       "linked_goal_id") if args.get(k) not in (None, "", [])}
        try:
            habit = client.create_habit(quest_id, strategy_id, name,
                                        frequency=str(args.get("frequency") or "daily"), **fields)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        if not habit:
            return ToolResult(ok=False, text=f"Nothing was created: the habit {name!r} was refused.")
        return ToolResult(ok=True, data=habit, text=f"Created the habit {name!r} on quest {quest_id}.")

    return ToolSpec(
        name="create_quest_habit",
        direct=True,
        handler=handler,
        description=_with_standard("Create one recurring habit on a quest, under one of its "
                                   "strategies.", "habit"),
        when_to_use="The user asked to add a habit or routine to their quest. " + _NEVER_CODE,
        when_not_to_use=("Logging that a habit was done today (that is a habit entry, not a new "
                         "habit). A one-off action (use create_quest_goal). A habit the user did "
                         "not ask for: suggest it instead."),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "name": {"type": "string", "description": "Verb-first name with the minimum dose."},
                "frequency": {"type": "string",
                              "enum": ["daily", "weekly", "monthly", "quarterly", "yearly"],
                              "description": "How often (default daily)."},
                "days": {"type": "array", "items": {"type": "string"},
                         "description": "For weekly: day names like ['Mon','Wed','Fri']."},
                "time": {"type": "string", "description": "Time of day, HH:MM."},
                "duration": {"type": "integer", "description": "Minutes per session."},
                "description": {"type": "string", "description": "Optional detail."},
                "strategy_id": {"type": "string",
                                "description": "Strategy it serves (defaults to the quest's first "
                                               "accepted strategy)."},
                "linked_goal_id": {"type": "string",
                                   "description": "Goal its entries count toward."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD (default today)."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_step=False,  # needs the user's own say-so (user_asked_for_this_field); a scheduled step cannot assert it
        origin="standard",
        keywords=("habit", "routine", "recurring", "daily", "weekly", "quest", "add", "create"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def _with_credentials(build: Callable[[Mapping[str, str]], ToolSpec]):
    return lambda env: build(env) if quest_credentials_present(env) else None


STANDARD_TOOL_BUILDERS.update({
    "send_quest_email": _with_credentials(send_quest_email_spec),
    "file_email_draft": _with_credentials(file_email_draft_spec),
    **{tool: _with_credentials(lambda env, f=field_name: quest_field_tool_spec(f, env))
       for field_name, tool in QUEST_FIELD_TOOLS.items()},
    "add_quest_measurable_outcome": _with_credentials(add_measurable_outcome_spec),
    "create_quest_goal": _with_credentials(create_quest_goal_spec),
    "add_quest_strategy": _with_credentials(add_quest_strategy_spec),
    "create_quest_habit": _with_credentials(create_quest_habit_spec),
})

#: Every standard tool that writes quest data, for guards and briefs that must name them.
QUEST_DATA_TOOLS: Tuple[str, ...] = (*QUEST_FIELD_TOOLS.values(), "add_quest_measurable_outcome",
                                     "create_quest_goal", "add_quest_strategy",
                                     "create_quest_habit", "create_quest_experiment",
                                     "update_quest_experiment", "delete_quest_experiment")
