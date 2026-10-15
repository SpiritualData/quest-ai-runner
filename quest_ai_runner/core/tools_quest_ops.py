"""Quest operations an AI run performs, defined once as registry tools.

Role: the single definition of "how an AI does X on Quest" for the operations that are not a
quest-field write (those live in ``tools``): raise an ask for a person, list the open ones, record
and close a request in the asks ledger, record outcome progress, leave a note, create or re-link a
goal. Each is a :class:`~quest_ai_runner.core.tools.ToolSpec`, so one definition (name, purpose,
when to use it, arguments, read or write) is rendered for every mode by the registry:

* a deep run sees ``python -m quest_ai_runner.tools call NAME --kw ...`` in its brief,
* the planner's ``tool`` action sees the same entry as a callable,
* ``quest-ai-runner quest --describe NAME`` and ``tools show NAME`` print it on demand,
* Quest Workflows could list it (these are withheld: ``workflow_step=False``).

An entry reaches a run only when the work calls for it: BM25 relevance (name and keywords count
double), or a pin the run's composer names in ``context_meta["tool_pins"]`` (an autopilot batch pins
the asks and progress operations because every pass owes them). A tool is named after the
``QuestClient`` method it wraps, so ``quest-ai-runner quest NAME`` and the tool agree.

Friendliness is built into the operations, not explained in prompts: ids are optional where words
identify the thing (an outcome by its text, an ask by a phrase of its words), the quest comes from
the run, a duplicate ask is recognised instead of raised twice, and a refusal says how to fix the
call. ``core`` imports no runner code; the Quest client arrives through the factory ``tools`` holds.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from .tools import (STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, quest_client_from_env,
                    quest_credentials_present)

#: Ask states that are finished or waiting on a person, not on an AI run. ``list_asks`` hides these
#: unless ``include_closed`` is set, so "what is open" is one call with no filter to remember.
CLOSED_ASK_STATES = frozenset({"done", "accepted", "declined", "noted", "superseded", "in_force",
                               "awaiting_acceptance", "awaiting_answer"})

#: The states an AI run may record on an ask (the backend refuses ``accepted``: only a person accepts).
RUN_ASK_STATES = ("in_progress", "done", "blocked", "declined", "noted", "awaiting_answer")

QUEST_ID_PARAM = {"type": "string", "description": "The quest (defaults to the quest this run is on)."}
NO_QUEST = ToolResult(ok=False, text="No quest is open in this run, so pass quest_id.")


def quest_id_of(args: Mapping[str, Any], ctx: ToolContext) -> Optional[str]:
    return str(args.get("quest_id") or ctx.quest_id or "") or None


def trim(text: Any, limit: int = 160) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit - 3].rstrip() + "..."


def words_of(text: Any) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def same_words(first: Any, second: Any, threshold: float = 0.8) -> bool:
    """Whether two texts say the same thing: equal word sets, or Jaccard overlap of at least ``threshold``."""
    a, b = words_of(first), words_of(second)
    return bool(a and b) and len(a & b) / len(a | b) >= threshold


def ask_key(row: Mapping[str, Any]) -> str:
    return str(row.get("key") or "")


def ask_line(row: Mapping[str, Any]) -> str:
    kind = f" {row['kind']}" if row.get("kind") else ""
    return f"- {ask_key(row)} [{row.get('state') or 'open'}{kind}] {trim(row.get('text'))}"


def open_asks(client, quest_id: str, include_closed: bool = False) -> Optional[List[Dict[str, Any]]]:
    """The quest's asks (open ones unless ``include_closed``), or None when the read failed."""
    rows = client.list_asks(quest_id=quest_id, limit=200)
    if rows is None:
        return None
    return [r for r in rows if include_closed or str(r.get("state") or "open") not in CLOSED_ASK_STATES]


# ---------------------------------------------------------------------------------------------
# Decision-requests: everything an AI needs from a person
# ---------------------------------------------------------------------------------------------

def create_decision_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return ToolResult(ok=False, text="Nothing was raised: summary is empty. Write what the "
                                             "person needs to answer, approve or do.")
        quest_id = quest_id_of(args, ctx)
        client = factory()
        for row in (client.list_open_decisions_for_quest(quest_id) if quest_id else []):
            if same_words(row.get("summary"), summary):
                ident = row.get("decision_id") or row.get("id")
                return ToolResult(ok=True, data=row, text=(
                    f"Already open as ask {ident}: not raised again. Point to it in your result "
                    f"instead of a second ask."))
        kwargs: Dict[str, Any] = {"kind": str(args.get("kind") or "approve").strip().lower(),
                                  "quest_id": quest_id}
        if args.get("review_url"):
            kwargs["review_url"] = str(args["review_url"])
        row = client.create_decision(summary, **kwargs)
        ident = (row or {}).get("decision_id") or (row or {}).get("id")
        if not ident:
            return ToolResult(ok=False, text=(
                "Quest did not accept the ask (the runner log has the reason), so the person has "
                "NOT been asked. Do not report it as raised; try once more with a shorter summary, "
                "or put it in your result as something you could not raise."))
        return ToolResult(ok=True, data=row, text=f"Raised ask {ident} for the person on quest "
                                                  f"{quest_id or '(no quest)'}.")

    return ToolSpec(
        name="create_decision",
        handler=handler,
        description=("Raise something you need from a person as a Quest ask: a question, an "
                     "approval, a choice or an action only they can do. It appears in their asks "
                     "with an Approve/Decline answer."),
        when_to_use=("You need anything from a person. One call per ask. Make it doable without "
                     "thinking: the link to open, the command to paste, the message to send and to "
                     "whom, and the options when it is a choice."),
        when_not_to_use=("A change to a quest field (its own tool files the ask). Something already "
                         "open: it is recognised and not raised twice. A report of finished work."),
        parameters={
            "type": "object",
            "required": ["summary"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "summary": {"type": "string",
                            "description": "What the person must answer, approve or do, in full."},
                "kind": {"type": "string",
                         "description": "approve (yes or no, the default), choose (list the options "
                                        "in summary), answer (a question), or do (an action)."},
                "review_url": {"type": "string",
                               "description": "A page to open to decide: a draft, document or PR."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("ask", "asks", "decision", "approval", "approve", "question", "person", "human",
                  "needs", "choose", "raise", "request"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def list_open_decisions_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        rows = factory().list_open_decisions_for_quest(quest_id)
        if not rows:
            return ToolResult(ok=True, data=[], text=f"No open asks for people on quest {quest_id}.")
        lines = [f"- {r.get('decision_id') or r.get('id')} [{r.get('kind') or 'approve'}] "
                 f"{trim(r.get('summary'))}" for r in rows]
        return ToolResult(ok=True, data=rows, text="Open asks for people on this quest:\n" + "\n".join(lines))

    return ToolSpec(
        name="list_open_decisions_for_quest",
        handler=handler,
        description="List the asks already waiting on a person on this quest (decision-requests).",
        when_to_use="Before raising an ask, or to point the person at what is already open.",
        when_not_to_use="To see what people asked of the AI: that is list_asks.",
        parameters={"type": "object", "properties": {"quest_id": QUEST_ID_PARAM}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("open", "asks", "decisions", "waiting", "pending", "approvals"),
        context_args={"quest_id": "quest_id"})


# ---------------------------------------------------------------------------------------------
# The asks ledger (What You Asked): requests people made of the AI
# ---------------------------------------------------------------------------------------------

def list_asks_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        rows = open_asks(factory(), quest_id, include_closed=bool(args.get("include_closed")))
        if rows is None:
            return ToolResult(ok=False, text="The asks ledger could not be read just now; try again, "
                                             "and do not assume the quest has none.")
        if not rows:
            return ToolResult(ok=True, data=[], text=f"No open asks on quest {quest_id}.")
        return ToolResult(ok=True, data=rows, text=(
            "What people asked on this quest (key [state kind] their words):\n"
            + "\n".join(ask_line(r) for r in rows)))

    return ToolSpec(
        name="list_asks",
        handler=handler,
        description=("List what people asked of the AI on this quest (the What You Asked ledger), "
                     "open ones first, each with its key."),
        when_to_use=("To work the open asks, to check whether a request is already recorded before "
                     "you record it, or to find the key update_ask needs."),
        when_not_to_use="To see asks waiting on a person: that is list_open_decisions_for_quest.",
        parameters={"type": "object", "properties": {
            "quest_id": QUEST_ID_PARAM,
            "include_closed": {"type": "boolean",
                               "description": "Also show finished, declined and answered ones."}}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("asks", "asked", "ledger", "requests", "what", "you", "asked", "open", "work"),
        context_args={"quest_id": "quest_id"})


def record_ask_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        text = str(args.get("text") or "").strip()
        if not quest_id:
            return NO_QUEST
        if not text:
            return ToolResult(ok=False, text="Nothing was recorded: text is empty. Pass the "
                                             "person's exact words.")
        client = factory()
        if not hasattr(client, "record_ask"):
            return ToolResult(ok=False, text="This Quest client cannot record asks yet; mention the "
                                             "request in a quest note instead (add_quest_note).")
        kwargs: Dict[str, Any] = {"kind": str(args.get("kind") or "request")}
        if args.get("author"):
            kwargs["author_id"] = str(args["author"])
        row = client.record_ask(quest_id, text, **kwargs)
        if not row or not ask_key(row):
            return ToolResult(ok=False, text="Quest did not record the ask (the runner log has the "
                                             "reason). Do not report it as recorded.")
        return ToolResult(ok=True, data=row, text=(
            f"Recorded as ask {ask_key(row)} [{row.get('state') or 'open'}]. Recording the same words "
            f"again is the same ask, so this is safe to repeat."))

    return ToolSpec(
        name="record_ask",
        handler=handler,
        description=("Record a request a person made of the AI (in a note, reply, chat message or "
                     "email) in the What You Asked ledger, in their exact words."),
        when_to_use=("You see a request that list_asks does not show. Record it BEFORE working on it, "
                     "so the person can see it was heard and later accept the result."),
        when_not_to_use=("Your own ideas or tasks (they are not requests). Something already listed. "
                         "A request you cannot attribute to a person."),
        parameters={
            "type": "object", "required": ["text"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "text": {"type": "string", "description": "The person's exact words, not a paraphrase."},
                "author": {"type": "string",
                           "description": "Who asked (their email); defaults to the account's owner."},
                "kind": {"type": "string",
                         "description": "request (default), standing (a rule that stays in force) or "
                                        "question."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("ask", "asked", "record", "request", "ledger", "heard", "person", "said", "wants"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def resolve_ask(rows: List[Dict[str, Any]], ask: str):
    """``(row, problem)``: the ask a key or a phrase of its words names, or a sentence listing them."""
    wanted = str(ask or "").strip()
    for row in rows:
        if ask_key(row) == wanted:
            return row, None
    low = wanted.lower()
    hits = [r for r in rows if low and low in str(r.get("text") or "").lower()]
    if len(hits) == 1:
        return hits[0], None
    listing = "\n".join(ask_line(r) for r in rows[:12]) or "(none open)"
    what = "matches several asks" if hits else "is not the key or words of an ask here"
    return None, f"{trim(wanted, 60)!r} {what}. Pass one of these keys:\n{listing}"


def update_ask_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        state = str(args.get("state") or "").strip().lower().replace(" ", "_")
        if state not in RUN_ASK_STATES:
            return ToolResult(ok=False, text=(
                f"Not updated: state must be one of {', '.join(RUN_ASK_STATES)} "
                f"('accepted' is only ever a person's). done means you finished it and the person "
                f"can now accept it."))
        client = factory()
        if not hasattr(client, "update_ask"):
            return ToolResult(ok=False, text="This Quest client cannot update asks yet; say where "
                                             "the ask stands in a quest note (add_quest_note).")
        ask = str(args.get("ask") or "").strip()
        if not ask:
            return ToolResult(ok=False, text="Not updated: ask is empty. Pass the ask's key from "
                                             "list_asks, or a phrase of its words.")
        rows = open_asks(client, quest_id, include_closed=True) if quest_id else None
        if rows is not None:
            row, problem = resolve_ask(rows, ask)
            if row is None:
                return ToolResult(ok=False, text=f"Not updated: {problem}")
            ask = ask_key(row)
        row = client.update_ask(ask, state, note=str(args.get("note") or ""))
        if not row:
            return ToolResult(ok=False, text="Quest did not update the ask (the runner log has the "
                                             "reason). Do not report it as done.")
        shown = row.get("state") or state
        return ToolResult(ok=True, data=row, text=(
            f"Ask {ask_key(row) or ask} is now {shown}."
            + (" The person can accept it." if shown == "awaiting_acceptance" else "")))

    return ToolSpec(
        name="update_ask",
        handler=handler,
        description=("Record where one of the person's asks stands, as the run's claim: in_progress, "
                     "done, blocked, declined, noted or awaiting_answer. A done ask waits for the "
                     "person to accept it; you never accept one."),
        when_to_use=("You started, finished or could not finish an open ask. Say what you did, or why "
                     "not, in note."),
        when_not_to_use="Marking a goal done (goals stay the person's). Asks you have not worked.",
        parameters={
            "type": "object", "required": ["ask", "state"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "ask": {"type": "string",
                        "description": "The ask's key (from list_asks) or a phrase of its words."},
                "state": {"type": "string", "description": " | ".join(RUN_ASK_STATES)},
                "note": {"type": "string",
                         "description": "One line of evidence: what you did, or what blocks it."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("ask", "asked", "done", "finished", "claim", "ledger", "accept", "progress", "blocked"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


# ---------------------------------------------------------------------------------------------
# Outcome progress, notes, goals
# ---------------------------------------------------------------------------------------------

def update_outcome_progress_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        outcome = str(args.get("outcome_id") or "").strip()
        if not outcome or args.get("progress_pct") in (None, "") or not str(args.get("note") or "").strip():
            return ToolResult(ok=False, text="Needs outcome_id (or its words), progress_pct (0 to 100) "
                                             "and note (one line naming concrete evidence).")
        try:
            result = factory().update_outcome_progress(
                quest_id, outcome, args.get("progress_pct"), str(args["note"]),
                current_value=args.get("current_value"))
        except Exception as e:  # the client's "Not done: <reason>" names what to change
            return ToolResult(ok=False, text=str(e))
        if (result or {}).get("pending_approval"):
            return ToolResult(ok=True, data=result, text=(
                f"Not written yet: it is waiting for a person to approve ({result.get('decision_id')}). "
                f"Do not report the number as recorded."))
        return ToolResult(ok=True, data=result, text=(
            f"Recorded {result.get('progress_pct', args.get('progress_pct'))}% on "
            f"{trim(result.get('text') or outcome, 80)!r}."))

    return ToolSpec(
        name="update_outcome_progress",
        handler=handler,
        description=("Record how far one of the quest's measurable outcomes has come, as a percentage "
                     "with one line of evidence. Never edits the outcome's words or its done box."),
        when_to_use=("You touched or learned something about a measurable outcome. Judge honestly "
                     "and never inflate; 100 means truly met. The note names concrete evidence (a goal, "
                     "file, metric or result). A refusal says why: rewrite the note with new "
                     "specifics, and after three refusals put where it stands in a quest note."),
        when_not_to_use="Rewording an outcome, or adding one (that is add_quest_measurable_outcome).",
        parameters={
            "type": "object", "required": ["outcome_id", "progress_pct", "note"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "outcome_id": {"type": "string",
                               "description": "The outcome's id, a distinctive part of its words, or "
                                              "its position (1 is the first)."},
                "progress_pct": {"type": "number", "description": "0 to 100."},
                "note": {"type": "string", "description": "One line naming the evidence for the number."},
                "current_value": {"type": "number",
                                  "description": "Where it stands now in the outcome's own unit."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("outcome", "outcomes", "progress", "measurable", "percent", "metric", "stands",
                  "record", "kpi"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def add_quest_note_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        text = str(args.get("text") or "").strip()
        if not quest_id:
            return NO_QUEST
        if not text:
            return ToolResult(ok=False, text="Nothing was written: text is empty.")
        notes = factory().add_quest_note(quest_id, text)
        if not notes:
            return ToolResult(ok=False, text="The note was not saved (the runner log has the reason).")
        return ToolResult(ok=True, data={"notes": len(notes)}, text=f"Added a note to quest {quest_id}.")

    return ToolSpec(
        name="add_quest_note",
        handler=handler,
        description="Add a note to the quest's thread, written as the AI (never as the person).",
        when_to_use=("Where an outcome stands when its progress write was refused, a finding the "
                     "person should see on the quest, or a hand-off to the next run."),
        when_not_to_use=("A request someone made (record_ask), something you need from them "
                         "(create_decision), or a result (that is your result)."),
        parameters={"type": "object", "required": ["text"], "properties": {
            "quest_id": QUEST_ID_PARAM,
            "text": {"type": "string", "description": "The note, in plain words."}}},
        mutates=True, workflow_step=False, origin="standard",
        keywords=("note", "notes", "thread", "write", "record", "finding", "hand", "off"),
        context_args={"quest_id": "quest_id"})


def list_quest_notes_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        limit = max(1, min(int(args.get("limit") or 15), 50))
        notes = list(factory().list_quest_notes(quest_id, limit=limit))
        if not notes:
            return ToolResult(ok=True, data=[], text=f"Quest {quest_id} has no notes.")
        lines = [f"- {n.get('author_label') or n.get('author_kind') or 'someone'} "
                 f"({str(n.get('created_at') or '')[:10]}): {trim(n.get('text'), 220)}" for n in notes[-limit:]]
        return ToolResult(ok=True, data=notes, text="Newest notes, oldest first:\n" + "\n".join(lines))

    return ToolSpec(
        name="list_quest_notes",
        handler=handler,
        description="Read the quest's newest notes (the person's replies and earlier runs' notes).",
        when_to_use="You need what was said on the quest recently and it is not in your brief.",
        when_not_to_use="The brief already lists them, or you need the asks (list_asks).",
        parameters={"type": "object", "properties": {
            "quest_id": QUEST_ID_PARAM,
            "limit": {"type": "integer", "description": "How many newest notes (default 15, at most 50)."}}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("notes", "thread", "replies", "said", "recent", "read"),
        context_args={"quest_id": "quest_id"})


def create_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        title = str(args.get("title") or "").strip()
        period = str(args.get("period") or "").strip()
        if not quest_id or not title or not period:
            return ToolResult(ok=False, text="A goal needs a quest, a title and a period.")
        client = factory()
        outcome = str(args.get("outcome_id") or "").strip()
        if outcome:
            resolved, problem = client.resolve_outcome_id(quest_id, outcome)
            if problem:
                return ToolResult(ok=False, text=f"Not created: {problem}")
            outcome = resolved or outcome
        kwargs = {k: args[k] for k in ("description", "criteria", "parent_goal_id", "target_value",
                                       "target_unit") if args.get(k) not in (None, "")}
        if outcome:
            kwargs["outcome_id"] = outcome
        try:
            goal = client.create_goal(title, period=period, quest_id=quest_id, **kwargs)
        except Exception as e:
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        ident = (goal or {}).get("id")
        if not ident:
            return ToolResult(ok=False, text="Nothing was created: Quest returned no goal.")
        return ToolResult(ok=True, data=goal, text=(
            f"Created the {period} goal {title!r} ({ident})" + (f", linked to outcome {outcome}." if outcome else ".")))

    return ToolSpec(
        name="create_goal",
        handler=handler,
        description=("Create a goal on the quest's plan because your standing instructions say to fill "
                     "a gap: one period, linked to the measurable outcome it moves, under its parent "
                     "goal, with a completion criterion a person can check."),
        when_to_use=("Autopilot work where a period has no goal for an outcome or the horizon under a "
                     "goal is empty. Create only what is missing."),
        when_not_to_use=("A goal the person asked for in conversation (create_quest_goal). Restating a "
                         "goal that exists. Marking any goal done."),
        parameters={
            "type": "object", "required": ["title", "period"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "title": {"type": "string", "description": "Verb-first goal title."},
                "period": {"type": "string",
                           "description": "YYYY-MM-DD (day), YYYY_W## (week), YYYY_MM (month), "
                                          "YYYY_Q# (quarter) or YYYY (year)."},
                "outcome_id": {"type": "string",
                               "description": "The measurable outcome this goal moves: its id, a "
                                              "distinctive part of its words, or its position."},
                "parent_goal_id": {"type": "string", "description": "The longer-period goal it serves."},
                "criteria": {"type": "string", "description": "How anyone can check it is done."},
                "description": {"type": "string", "description": "Optional detail."},
                "target_value": {"type": "number", "description": "The outcome's number carried in."},
                "target_unit": {"type": "string", "description": "The outcome's unit carried in."},
            },
        },
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("goal", "goals", "plan", "outcome", "period", "parent", "link", "missing", "gap"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def update_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))
    editable = ("outcome_id", "parent_goal_id", "criteria", "description")

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        goal_id = str(args.get("goal_id") or "").strip()
        quest_id = quest_id_of(args, ctx)
        fields = {k: args[k] for k in editable if args.get(k) not in (None, "")}
        if not goal_id or not fields:
            return ToolResult(ok=False, text=f"Needs goal_id and at least one of {', '.join(editable)}.")
        client = factory()
        if "outcome_id" in fields and quest_id:
            resolved, problem = client.resolve_outcome_id(quest_id, str(fields["outcome_id"]))
            if problem:
                return ToolResult(ok=False, text=f"Not changed: {problem}")
            fields["outcome_id"] = resolved or fields["outcome_id"]
        goal = client.update_goal(goal_id, fields)
        if not goal:
            return ToolResult(ok=False, text=("Quest refused the change (the runner log has the reason). "
                                              "Check goal_id, and that outcome_id belongs to this quest."))
        return ToolResult(ok=True, data=goal, text=f"Updated goal {goal_id}: {', '.join(sorted(fields))}.")

    return ToolSpec(
        name="update_goal",
        handler=handler,
        description=("Link an existing goal to the measurable outcome it moves, or fix its parent goal, "
                     "completion criterion or description. Never its title or its done state."),
        when_to_use="A goal in the plan has no outcome link, or points at the wrong one.",
        when_not_to_use="Completing a goal (goals stay the person's), renaming one, or creating one.",
        parameters={"type": "object", "required": ["goal_id"], "properties": {
            "quest_id": QUEST_ID_PARAM,
            "goal_id": {"type": "string", "description": "The goal to change."},
            "outcome_id": {"type": "string",
                           "description": "The outcome it moves: id, distinctive words, or position."},
            "parent_goal_id": {"type": "string", "description": "The longer-period goal it serves."},
            "criteria": {"type": "string", "description": "How anyone can check it is done."},
            "description": {"type": "string", "description": "Detail."}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("goal", "goals", "link", "outcome", "parent", "criteria", "relink", "plan"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


#: Registered into the standard catalog (``tools.STANDARD_TOOL_BUILDERS``) when this module loads.
QUEST_OPERATION_BUILDERS = {
    "create_decision": with_credentials(create_decision_spec),
    "list_open_decisions_for_quest": with_credentials(list_open_decisions_spec),
    "list_asks": with_credentials(list_asks_spec),
    "record_ask": with_credentials(record_ask_spec),
    "update_ask": with_credentials(update_ask_spec),
    "update_outcome_progress": with_credentials(update_outcome_progress_spec),
    "add_quest_note": with_credentials(add_quest_note_spec),
    "list_quest_notes": with_credentials(list_quest_notes_spec),
    "create_goal": with_credentials(create_goal_spec),
    "update_goal": with_credentials(update_goal_spec),
}
STANDARD_TOOL_BUILDERS.update(QUEST_OPERATION_BUILDERS)

#: Operations every autopilot batch is pinned to (see ``runner.run_operations``): the asks the pass
#: works first and the progress it owes on every pass.
AUTOPILOT_PINS = ("list_asks", "record_ask", "update_ask", "create_decision", "update_outcome_progress")
