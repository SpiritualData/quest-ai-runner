"""Quest operations that keep the plan true as work spawns and changes hands.

Role: three registry tools (see ``tools_quest_ops`` for how the registry shows an operation to a
run). ``create_task`` starts follow-up AI work and makes it declare, in structured fields, which
goal it serves: an existing goal (``related_goal_id``) or a new one created with the task
(``new_goal``), so spawned work never floats free of the quest's plan. ``assign_goal`` hands a goal
to a quest member with the reason and the next step, which the new owner's AI reads as the goal's
latest update. ``complete_goal`` ticks a goal done, the same as the app's checkbox.

Nothing here decides by keywords in a model's output. The model fills fields; the only judgement
code makes is which existing goals look like the proposed new one, and that only decides whether
to show them to the model first (it then states in ``distinct_from_existing`` that none fits).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .tools import STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, quest_client_from_env
from .tools import quest_credentials_present
from .tools_quest_ops import (NO_QUEST, QUEST_OPERATION_BUILDERS, QUEST_ID_PARAM, flat_goals, goal_line, goal_row, quest_id_of,
                              words_of)

#: How much of a proposed goal's title another goal must cover to be shown as "may already stand for it".
SIMILAR_TITLE_COVERAGE = 0.7
SIMILAR_TITLE_MIN_SHARED = 2
SIMILAR_GOALS_SHOWN = 5
FILLER_WORDS = frozenset({"the", "a", "an", "to", "of", "for", "and", "on", "in", "with", "by", "our", "their"})


def title_words(title: Any) -> frozenset:
    return frozenset(w for w in words_of(title) if w not in FILLER_WORDS)


def similar_open_goals(title: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Open goals whose title covers most of ``title``'s words, best first."""
    wanted = title_words(title)
    scored = []
    for row in rows:
        if row["completed"] or row["abandoned"]:
            continue
        theirs = title_words(row["title"])
        shared = wanted & theirs
        if len(shared) < min(SIMILAR_TITLE_MIN_SHARED, len(wanted), len(theirs)) or not shared:
            continue
        coverage = len(shared) / max(1, min(len(wanted), len(theirs)))
        if coverage >= SIMILAR_TITLE_COVERAGE:
            scored.append((coverage, row))
    scored.sort(key=lambda pair: -pair[0])
    return [row for _, row in scored[:SIMILAR_GOALS_SHOWN]]


def resolve_member(client: Any, quest_id: str, who: str):
    """``(user_id, problem)`` for a quest member named by id, name or email fragment."""
    members = client.list_assignable_members(quest_id) or []
    wanted = str(who).strip().lower()
    for m in members:
        if str(m.get("user_id")) == str(who).strip():
            return str(m["user_id"]), None
    hits = [m for m in members if wanted and (wanted in str(m.get("name") or "").lower()
                                              or wanted == str(m.get("email") or "").lower())]
    roster = ", ".join(f"{m.get('name') or m.get('email')} ({m.get('user_id')})" for m in members) or "none found"
    if len(hits) == 1:
        return str(hits[0]["user_id"]), None
    if not hits:
        return None, f"Nobody on this quest matches {who!r}. The people it can be assigned to: {roster}."
    return None, f"{who!r} matches more than one person: {roster}. Pass the user_id."


def current_task_goal(client: Any, ctx: ToolContext) -> Dict[str, Any]:
    """The run's own task, read once: its quest and the goal inside it that it serves."""
    if not ctx.task_id:
        return {}
    try:
        task = client.get_task(ctx.task_id) or {}
    except Exception:  # noqa: BLE001 - a failed read means no default, never a failed create
        return {}
    return {"quest_id": task.get("goal_id"), "related_goal_id": task.get("related_goal_id")}


def create_task_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, text="A task needs text: the work to do, in full.")
        client = factory()
        current = current_task_goal(client, ctx)
        quest_id = str(args.get("quest_id") or ctx.quest_id or current.get("quest_id") or "") or None
        related = str(args.get("related_goal_id") or "").strip()
        new_goal = args.get("new_goal") if isinstance(args.get("new_goal"), dict) else None
        if related and new_goal:
            return ToolResult(ok=False, text="Send related_goal_id or new_goal, not both.")
        kwargs: Dict[str, Any] = {}
        served = ""
        if quest_id:
            kwargs["goal_id"] = quest_id
            if new_goal:
                title = str(new_goal.get("title") or "").strip()
                if not title:
                    return ToolResult(ok=False, text="new_goal needs a title.")
                spec: Dict[str, Any] = {"title": title}
                for key in ("deadline", "description", "criteria"):
                    if new_goal.get(key) not in (None, ""):
                        spec[key] = new_goal[key]
                if not new_goal.get("distinct_from_existing"):
                    payload = client.list_quest_goals(quest_id)
                    if isinstance(payload, dict) and "period_groups" in payload:
                        rows = [goal_row(g, {}) for g in flat_goals(payload)]
                        near = similar_open_goals(title, rows)
                        if near:
                            return ToolResult(ok=False, text=(
                                "Nothing was created. These open goals may already stand for this work:\n"
                                + "\n".join(goal_line(r) for r in near)
                                + "\nIf one fits, call create_task again with its id as related_goal_id. "
                                  "If none does, call again with new_goal.distinct_from_existing=true."))
                if new_goal.get("outcome_id"):
                    resolved, problem = client.resolve_outcome_id(quest_id, str(new_goal["outcome_id"]))
                    if problem:
                        return ToolResult(ok=False, text=f"Nothing was created: {problem}")
                    spec["outcome_id"] = resolved or new_goal["outcome_id"]
                if new_goal.get("parent_goal_id"):
                    spec["parent_goal_id"] = new_goal["parent_goal_id"]
                if new_goal.get("assigned_to_user_id"):
                    member, problem = resolve_member(client, quest_id, str(new_goal["assigned_to_user_id"]))
                    if problem:
                        return ToolResult(ok=False, text=f"Nothing was created: {problem}")
                    spec["assigned_to_user_id"] = member
                kwargs["new_goal"] = spec
            elif related:
                kwargs["related_goal_id"] = related
                served = related
            elif current.get("related_goal_id"):
                # The follow-up serves the goal the task that spawned it served.
                kwargs["related_goal_id"] = served = str(current["related_goal_id"])
            else:
                return ToolResult(ok=False, text=(
                    "Nothing was created. Work on a quest must say which goal it serves: pass related_goal_id "
                    "(an existing goal; list_quest_goals shows them) or new_goal (title, and where known "
                    "assigned_to_user_id, deadline, outcome_id) to create one under the goal you are working on."))
        elif related or new_goal:
            return ToolResult(ok=False, text="A goal belongs to a quest, and this run has none: pass quest_id.")
        if ctx.task_id:
            kwargs["parent_task_id"] = ctx.task_id
        for key in ("title", "scheduled_date", "scheduled_time", "assignee_user_id"):
            if args.get(key) not in (None, ""):
                kwargs[key] = args[key]
        try:
            task = client.create_task(text, source="chat", **kwargs)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        ident = (task or {}).get("id") or (task or {}).get("task_id")
        if not ident:
            return ToolResult(ok=False, text="Nothing was created: Quest returned no task.")
        spawned = (task or {}).get("spawned_goal") or {}
        if spawned:
            verb = "reused the open goal" if spawned.get("reused") else "created the goal"
            tail = f" It {verb} {spawned.get('name')!r} ({spawned.get('id')})."
        elif served:
            tail = f" It serves goal {served}."
        else:
            tail = ""
        return ToolResult(ok=True, data=task, text=f"Created task {ident}.{tail}")

    return ToolSpec(
        name="create_task",
        handler=handler,
        description=("Start follow-up AI work, and say which goal of the quest it serves so the plan "
                     "never misses it: an existing goal (related_goal_id) or a new one created with "
                     "the task (new_goal), which nests under the goal you are working on."),
        when_to_use=("Your work spawns more work, or finds work for someone else's AI lane. Look at the "
                     "quest's goals first (list_quest_goals) and use the goal that already stands for the work."),
        when_not_to_use=("Work you can finish in this run. A change to the plan with no work behind it "
                         "(that is a goal). Asking a person for something (create_decision)."),
        parameters={"type": "object", "required": ["text"], "properties": {
            "quest_id": QUEST_ID_PARAM,
            "text": {"type": "string", "description": "The work, in full: what to do and what done looks like."},
            "title": {"type": "string", "description": "Short label for the task."},
            "related_goal_id": {"type": "string", "description": "The existing goal this work serves."},
            "new_goal": {"type": "object", "description": (
                "A new goal for this work, created with the task: title (verb first, required), "
                "assigned_to_user_id (a quest member: id, name or email), deadline (YYYY-MM-DD), "
                "outcome_id (the outcome it moves), parent_goal_id (default: the goal you are working on), "
                "description, criteria, and distinct_from_existing=true once you have seen the open goals "
                "that look similar and none fits."), "properties": {
                    "title": {"type": "string"}, "assigned_to_user_id": {"type": "string"},
                    "deadline": {"type": "string"}, "outcome_id": {"type": "string"},
                    "parent_goal_id": {"type": "string"}, "description": {"type": "string"},
                    "criteria": {"type": "string"}, "distinct_from_existing": {"type": "boolean"}}},
            "assignee_user_id": {"type": "string", "description": "A team member whose AI lane runs the task (a user id)."},
            "scheduled_date": {"type": "string", "description": "YYYY-MM-DD."},
            "scheduled_time": {"type": "string", "description": "HH:MM."}}},
        mutates=True, workflow_step=False, origin="standard",
        keywords=("task", "tasks", "follow", "spawn", "spawned", "followup", "subtask", "queue",
                  "delegate", "goal"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=60.0)


def assign_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        goal_id = str(args.get("goal_id") or "").strip()
        who = str(args.get("assigned_to_user_id") or "").strip()
        reason = str(args.get("reason") or "").strip()
        next_step = str(args.get("next_step") or "").strip()
        quest_id = quest_id_of(args, ctx)
        if not goal_id or not who:
            return ToolResult(ok=False, text="Needs goal_id and assigned_to_user_id (a quest member: id, name or email).")
        if not reason or not next_step:
            return ToolResult(ok=False, text=("Not handed over. Say why the goal is changing hands (reason) and what "
                                              "the new owner does first (next_step): their AI guides them from it."))
        client = factory()
        if not quest_id:
            return NO_QUEST
        member, problem = resolve_member(client, quest_id, who)
        if problem:
            return ToolResult(ok=False, text=f"Not handed over: {problem}")
        try:
            goal = client.assign_goal(goal_id, member, reason=reason, next_step=next_step)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, text=f"Not handed over: {e}")
        if not goal:
            return ToolResult(ok=False, text="Not handed over: Quest returned nothing. Check goal_id.")
        carried = ""
        task_id_list = []
        try:
            tasks = client.list_tasks(goal_id=quest_id) or []
            task_id_list = [t for t in tasks if t.get("related_goal_id") == goal_id
                            and str(t.get("assignee_user_id") or t.get("user_id") or "") == member
                            and t.get("status") in ("queued", "suggested")]
        except Exception:  # noqa: BLE001
            tasks = []
        if task_id_list:
            carried = f" {len(task_id_list)} open AI task(s) on it now run in their lane."
        return ToolResult(ok=True, data=goal, text=(
            f"Handed goal {goal_id} to {member}. They were notified and a hand-over update (why, and what is "
            f"next) is on the goal for their AI to read.{carried} Open tasks whose owner cannot take them stay put."))

    return ToolSpec(
        name="assign_goal",
        handler=handler,
        description=("Hand a goal to a teammate on the quest, with why and what they do first. They are "
                     "notified, the goal shows on their dashboard with a hand-over update their AI reads "
                     "to guide them, and the goal's open AI tasks move to their lane."),
        when_to_use="Work rolls over to another person, or a goal belongs with someone else now.",
        when_not_to_use=("Naming who does a new goal (new_goal on create_task, or create_goal's assignee). "
                         "Giving work to an AI persona: assignees are people."),
        parameters={"type": "object", "required": ["goal_id", "assigned_to_user_id", "reason", "next_step"],
                    "properties": {
            "quest_id": QUEST_ID_PARAM,
            "goal_id": {"type": "string", "description": "The goal to hand over."},
            "assigned_to_user_id": {"type": "string", "description": "The quest member taking it: id, name or email."},
            "reason": {"type": "string", "description": "Why it is changing hands."},
            "next_step": {"type": "string", "description": "What the new owner does first."}}},
        mutates=True, workflow_step=False, origin="standard",
        keywords=("assign", "reassign", "hand", "handover", "handoff", "roll", "rollover", "teammate",
                  "owner", "goal"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=60.0)


def complete_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        goal_id = str(args.get("goal_id") or "").strip()
        if not goal_id:
            return ToolResult(ok=False, text="Needs goal_id.")
        completed = args.get("completed", True) is not False
        client = factory()
        goal = client.complete_goal(goal_id, completed=completed)
        if not goal:
            return ToolResult(ok=False, text=("Quest refused the change (the runner log has the reason). "
                                              "Check goal_id, and that you can edit this goal's quest."))
        return ToolResult(ok=True, data=goal, text=f"Marked goal {goal_id} {'done' if completed else 'not done'}.")

    return ToolSpec(
        name="complete_goal",
        handler=handler,
        description="Mark a goal done (or not done), the same as ticking it in the app: the owner is credited.",
        when_to_use="A person asked you to close a goal, or its owner said it is done.",
        when_not_to_use="Your own judgement that a goal looks finished: goals stay the person's.",
        parameters={"type": "object", "required": ["goal_id"], "properties": {
            "goal_id": {"type": "string", "description": "The goal to close."},
            "completed": {"type": "boolean", "description": "False to reopen it. Default true."}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("complete", "done", "finish", "close", "goal", "tick"),
        timeout_seconds=45.0)


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


QUEST_WORK_BUILDERS = {
    "create_task": with_credentials(create_task_spec),
    "assign_goal": with_credentials(assign_goal_spec),
    "complete_goal": with_credentials(complete_goal_spec),
}
STANDARD_TOOL_BUILDERS.update(QUEST_WORK_BUILDERS)
QUEST_OPERATION_BUILDERS.update(QUEST_WORK_BUILDERS)  # one list of Quest operations

#: Operations an autopilot pass is pinned to in addition to ``tools_quest_ops.AUTOPILOT_PINS``: it
#: spawns and hands over work, so these never wait on a lexical match.
AUTOPILOT_WORK_PINS = ("create_task", "assign_goal")
