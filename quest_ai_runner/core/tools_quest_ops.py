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
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional

from .tools import (STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, quest_client_from_env,
                    quest_credentials_present)

#: Ask states that are finished or waiting on a person, not on an AI run. ``list_asks`` hides these
#: unless ``include_closed`` is set, so "what is open" is one call with no filter to remember.
CLOSED_ASK_STATES = frozenset({"done", "accepted", "declined", "noted", "superseded", "in_force",
                               "awaiting_acceptance", "awaiting_answer"})

#: The states an AI run may record on an ask (the backend refuses ``accepted``: only a person accepts).
RUN_ASK_STATES = ("in_progress", "done", "blocked", "declined", "noted", "awaiting_answer")

#: The kinds an AI run may sort an ask into (a person's own sorting always wins).
RUN_ASK_KINDS = ("request", "standing", "context", "question")

QUEST_ID_PARAM = {"type": "string", "description": "The quest (defaults to the quest this run is on)."}
NO_QUEST = ToolResult(ok=False, text=("No quest is open in this run, so pass quest_id. If you do not know the id, "
                                      "call find_quest with words from the quest's name: do not retry this call "
                                      "without one."))


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


def ask_state(row: Mapping[str, Any]) -> str:
    """A row's state in the underscore spelling this module uses ("awaiting acceptance" -> "awaiting_acceptance")."""
    return str(row.get("state") or "open").strip().lower().replace(" ", "_")


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
    return [r for r in rows
            if include_closed or ask_state(r) not in CLOSED_ASK_STATES]


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
        if (row or {}).get("duplicate") == "answered":
            # The person already passed on, or declined, this same question. Quest's message says
            # what they decided and what to do instead; repeating it in new words is the loop.
            return ToolResult(ok=True, data=row, text=str(row.get("message") or (
                "Not asked: the person already passed on or declined this question. Do not ask it "
                "again in other words; use your own judgment or report what you could not do.")))
        if (row or {}).get("duplicate") is True:
            return ToolResult(ok=True, data=row, text=str(row.get("message") or (
                f"Already open as ask {row.get('decision_id')}: not raised again. Point to it in "
                f"your result instead of a second ask.")))
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
                         "open: it is recognised and not raised twice. A report of finished work. A "
                         "question about one of the person's asks in What You Asked (that is "
                         "update_ask with awaiting_answer and question, which links the two)."),
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
        # Name who each ask waits on: without it a reminder for one person cannot tell their asks
        # from everyone else's on the quest (seen 2026-10-16: it listed all 15 as Joshua's).
        lines = [f"- {r.get('decision_id') or r.get('id')} [{r.get('kind') or 'approve'}] "
                 f"(waiting on {r.get('assignee_name') or 'nobody named'}) {trim(r.get('summary'))}" for r in rows]
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
                                        "question or context (background)."},
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
        kind = str(args.get("kind") or "").strip().lower()
        if kind and kind not in RUN_ASK_KINDS:
            return ToolResult(ok=False, text=f"Not updated: kind must be one of "
                                             f"{', '.join(RUN_ASK_KINDS)}.")
        if not state and not kind:
            return ToolResult(ok=False, text="Not updated: pass a state, a kind, or both.")
        if state and state not in RUN_ASK_STATES:
            return ToolResult(ok=False, text=(
                f"Not updated: state must be one of {', '.join(RUN_ASK_STATES)} "
                f"('accepted' is only ever a person's). done means you finished it and the person "
                f"can now accept it."))
        question = str(args.get("question") or "").strip()
        if state == "awaiting_answer" and not question:
            return ToolResult(ok=False, text=(
                "Not updated: awaiting_answer needs the question, because it is raised to the person "
                "in their Asks for You. Call update_ask again with ask, state=awaiting_answer and "
                "question=what you need to know, in one or two plain sentences (with the options "
                "when it is a choice)."))
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
        extra = {"question": question} if question else {}
        row = client.update_ask(ask, state or None, note=str(args.get("note") or ""),
                                kind=kind or None, **extra)
        if not row:
            return ToolResult(ok=False, text="Quest did not update the ask (the runner log has the "
                                             "reason). Do not report it as done.")
        shown = ask_state(row) if row.get("state") else state
        sorted_as = f", sorted as {row.get('kind') or kind}" if kind else ""
        follow = ""
        if shown == "awaiting_acceptance":
            follow = " The person can accept it."
        elif shown == "awaiting_answer":
            follow = (f" Your question is now in the person's Asks for You"
                      f"{' (decision ' + str(row['decision_id']) + ')' if row.get('decision_id') else ''}"
                      f". Do not work this ask or ask again until they answer; their answer will be "
                      f"on the ask in your next brief.")
        return ToolResult(ok=True, data=row, text=(
            f"Ask {ask_key(row) or ask} is now {shown.replace('_', ' ')}{sorted_as}.{follow}"))

    return ToolSpec(
        name="update_ask",
        handler=handler,
        description=("Record where one of the person's asks stands, as the run's claim: in_progress, "
                     "done, blocked, declined or noted. A done ask waits for the person to accept "
                     "it; you never accept one. awaiting_answer, with the question, is how you ask "
                     "the person to clarify an ask: it raises the question in their Asks for You "
                     "and parks the ask until they answer. Also sorts an ask that is not sorted "
                     "yet (kind)."),
        when_to_use=("You started, finished or could not finish an open ask (say what you did, or "
                     "why not, in note), an ask shows NOT SORTED and you can tell what it is, or you "
                     "cannot finish an ask without the person clarifying it (state awaiting_answer "
                     "plus question; their answer comes back on the ask in your next brief)."),
        when_not_to_use=("Marking a goal done (goals stay the person's). Asks you have not worked. "
                         "A question that is not about one of their asks (that is create_decision)."),
        parameters={
            "type": "object", "required": ["ask"],
            "properties": {
                "quest_id": QUEST_ID_PARAM,
                "ask": {"type": "string",
                        "description": "The ask's key (from list_asks) or a phrase of its words."},
                "state": {"type": "string", "description": " | ".join(RUN_ASK_STATES)},
                "kind": {"type": "string",
                         "description": " | ".join(RUN_ASK_KINDS) + ". Sorts an ask that is not "
                                        "sorted yet, without changing its state."},
                "note": {"type": "string",
                         "description": "One line of evidence: what you did, or what blocks it."},
                "question": {"type": "string",
                             "description": "Required with state awaiting_answer: what you need the "
                                            "person to answer, in plain sentences, no em dashes."},
            },
        },
        mutates=True, workflow_step=False, origin="standard",
        keywords=("ask", "asked", "done", "finished", "claim", "ledger", "accept", "progress", "blocked",
                  "clarify", "clarification", "question"),
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

#: ``list_quest_goals`` output caps: a quest can hold hundreds of goals, so a call shows one page.
GOALS_DEFAULT_LIMIT = 25
GOALS_MAX_LIMIT = 100
GOAL_TEXT_CHARS = 320


def today_date() -> date:
    return date.today()


def day_of(value: Any) -> Optional[date]:
    """The calendar day in an ISO date or datetime string, or None."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def goal_due_day(goal: Mapping[str, Any]) -> Optional[date]:
    """The day a goal is meant to happen: its scheduled day, else the end of its period."""
    return day_of(goal.get("scheduled_date")) or day_of(goal.get("deadline"))


def flat_goals(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group in payload.get("period_groups") or []:
        out.extend(g for g in (group.get("goals") or []) if isinstance(g, dict))
    return out


def goal_row(goal: Mapping[str, Any], names: Mapping[str, str]) -> Dict[str, Any]:
    assignee = goal.get("assigned_to_user_id")
    due = goal_due_day(goal)
    return {"id": goal.get("id"), "title": goal.get("name") or "", "text": goal.get("description") or "",
            "assignee_user_id": assignee, "assignee_name": names.get(str(assignee or "")) or None,
            "completed": bool(goal.get("completed")), "abandoned": bool(goal.get("abandoned")),
            "time_scope": goal.get("time_scope"), "period": goal.get("period"),
            "due": due.isoformat() if due else None, "parent_goal_id": goal.get("parent_goal_id")}


def goal_line(row: Mapping[str, Any]) -> str:
    who = row.get("assignee_name") or row.get("assignee_user_id") or "unassigned"
    state = "done" if row.get("completed") else ("abandoned" if row.get("abandoned") else "open")
    text = f" | {trim(row['text'], GOAL_TEXT_CHARS)}" if row.get("text") else ""
    return (f"- {row['id']} [{state}] due {row.get('due') or 'no date'} ({row.get('time_scope')}) "
            f"for {who}: {trim(row['title'], 120)}{text}")


def read_failure_reason(client: Any) -> str:
    """Why the client's last read failed, in a few words, or a pointer to the runner log."""
    reason = " ".join(str(getattr(client, "last_error", "") or "").split())
    return reason[:160] if reason else "the runner log has the reason"


def list_quest_goals_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        client = factory()
        payload = client.list_quest_goals(quest_id)
        if not isinstance(payload, dict) or "period_groups" not in payload:
            return ToolResult(ok=False, text=(
                f"The goals of quest {quest_id} could not be read just now "
                f"({read_failure_reason(client)}). Do not assume the quest has none, and do not fill "
                f"in goal lines you have not seen: say they could not be read."))
        names = {str(m.get("user_id")): str(m.get("name") or "")
                 for m in (client.list_assignable_members(quest_id) or []) if m.get("user_id")}
        rows = [goal_row(g, names) for g in flat_goals(payload)]
        total = len(rows)
        if args.get("open_only", True):
            rows = [r for r in rows if not r["completed"] and not r["abandoned"]]
        if args.get("assignee_user_id"):
            rows = [r for r in rows if r["assignee_user_id"] == str(args["assignee_user_id"])]
        needle = str(args.get("text_contains") or "").strip().lower()
        if needle:
            rows = [r for r in rows if needle in f"{r['title']}\n{r['text']}".lower()]
        if args.get("due_within_days") is not None:
            first = today_date()
            last = first + timedelta(days=max(0, int(args["due_within_days"])))
            early = None if args.get("include_overdue") else first
            rows = [r for r in rows if r["due"] and day_of(r["due"]) <= last
                    and (early is None or day_of(r["due"]) >= early)]
        rows.sort(key=lambda r: (r["due"] is None, r["due"] or "", str(r["id"])))
        limit = max(1, min(int(args.get("limit") or GOALS_DEFAULT_LIMIT), GOALS_MAX_LIMIT))
        offset = max(0, int(args.get("offset") or 0))
        page = rows[offset:offset + limit]
        if not rows:
            return ToolResult(ok=True, data=[], text=(
                f"No goals on quest {quest_id} match ({total} goals in all, before filters)."))
        more = len(rows) - offset - len(page)
        head = (f"{len(rows)} matching goals on quest {quest_id} ({total} in all); showing "
                f"{offset + 1}-{offset + len(page)}, soonest due first"
                + (f". {more} more: call again with offset={offset + len(page)}." if more > 0 else "."))
        return ToolResult(ok=True, data=page, text=head + "\n" + "\n".join(goal_line(r) for r in page))

    return ToolSpec(
        name="list_quest_goals",
        handler=handler,
        description=("List the goals on a quest with their id, title, text (the brief, which may "
                     "start with an owner line), who they are assigned to (id and name), done or "
                     "open, time scope, due day and parent. Filter by assignee, due window or words; "
                     "one page at a time."),
        when_to_use=("You need the real goals of a quest: to remind someone of their open goals, "
                     "report what is due, find the goals a person owns, or look a goal up by its "
                     "words. Read it BEFORE writing any message that lists goals."),
        when_not_to_use=("Whole-portfolio views across quests. A write to a goal (update_goal, "
                         "create_goal). Never write goal lines into a message without reading them here."),
        parameters={"type": "object", "properties": {
            "quest_id": QUEST_ID_PARAM,
            "assignee_user_id": {"type": "string", "description": "Only goals assigned to this user id."},
            "open_only": {"type": "boolean",
                          "description": "Hide done and abandoned goals (default true)."},
            "due_within_days": {"type": "integer",
                                "description": "Only goals due from today through this many days ahead."},
            "include_overdue": {"type": "boolean",
                                "description": "With due_within_days, also goals already past due."},
            "text_contains": {"type": "string",
                              "description": "Only goals whose title or text contains this (case-insensitive), "
                                             "for example an owner line."},
            "limit": {"type": "integer",
                      "description": f"Page size (default {GOALS_DEFAULT_LIMIT}, at most {GOALS_MAX_LIMIT})."},
            "offset": {"type": "integer", "description": "Skip this many matches (next page)."}}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("goals", "goal", "list", "read", "due", "open", "assigned", "owner", "tasks",
                  "week", "reminder", "deadline", "assignee", "plan", "items"),
        context_args={"quest_id": "quest_id"})


#: ``list_quest_workflows`` output cap: a workflow can hold dozens of steps, so a call shows one page.
WORKFLOW_STEPS_DEFAULT_LIMIT = 30
WORKFLOW_STEPS_MAX_LIMIT = 100


def step_actor_name(actor: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """Who a workflow step belongs to, in words: a person's name, an AI rep, or a seat."""
    user = str(actor.get("userId") or "")
    if user:
        return names.get(user) or actor.get("label") or user
    if actor.get("repId"):
        return actor.get("label") or "an AI rep"
    return actor.get("label") or actor.get("seat") or "nobody named"


def step_rows(workflow: Mapping[str, Any], names: Mapping[str, str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for step in workflow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        actor = step.get("actor") or {}
        window = step.get("window") or {}
        rows.append({
            "workflow_id": workflow.get("workflowId"), "workflow_title": workflow.get("title") or "",
            "step_id": step.get("stepId"), "title": step.get("title") or "",
            "instructions": step.get("instructions") or "",
            "actor_kind": actor.get("kind"), "assignee_user_id": actor.get("userId"),
            "assignee_name": step_actor_name(actor, names),
            "status": str(step.get("status") or ""), "window_starts": window.get("startsAt"),
            "due": window.get("endsAt") or window.get("startsAt"),
            "completed_at": step.get("completedAt")})
    return rows


def step_line(row: Mapping[str, Any]) -> str:
    window = f"{row.get('window_starts') or '?'} to {row.get('due') or '?'}"
    return (f"- {row['step_id']} [{row['status'] or 'unknown'}] for {row['assignee_name']}, window {window}: "
            f"{trim(row['title'], 120)}")


def list_quest_workflows_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        client = factory()
        workflows = client.list_quest_workflows(quest_id)
        if workflows is None:
            return ToolResult(ok=False, text=(
                f"The workflows of quest {quest_id} could not be read just now "
                f"({read_failure_reason(client)}). Do not assume it has none, and do not state any "
                f"step as fact: say they could not be read."))
        if not workflows:
            return ToolResult(ok=True, data=[], text=f"Quest {quest_id} has no workflows.")
        names = {str(m.get("user_id")): str(m.get("name") or "")
                 for m in (client.list_assignable_members(quest_id) or []) if m.get("user_id")}
        rows = [r for wf in workflows for r in step_rows(wf, names)]
        total = len(rows)
        if args.get("open_only", True):
            rows = [r for r in rows if r["status"] != "done"]
        if args.get("status"):
            wanted = {w.strip().lower() for w in str(args["status"]).split(",") if w.strip()}
            rows = [r for r in rows if r["status"].lower() in wanted]
        who = str(args.get("assignee") or "").strip().lower()
        if who:
            rows = [r for r in rows if who == str(r["assignee_user_id"] or "").lower()
                    or who in r["assignee_name"].lower()]
        needle = str(args.get("text_contains") or "").strip().lower()
        if needle:
            rows = [r for r in rows if needle in f"{r['step_id']}\n{r['title']}\n{r['instructions']}".lower()]
        rows.sort(key=lambda r: (r["due"] is None, r["due"] or "", str(r["step_id"])))
        limit = max(1, min(int(args.get("limit") or WORKFLOW_STEPS_DEFAULT_LIMIT), WORKFLOW_STEPS_MAX_LIMIT))
        offset = max(0, int(args.get("offset") or 0))
        page = rows[offset:offset + limit]
        if not rows:
            return ToolResult(ok=True, data=[], text=(
                f"No workflow steps on quest {quest_id} match ({total} steps in all, before filters)."))
        more = len(rows) - offset - len(page)
        head = (f"{len(rows)} matching workflow steps on quest {quest_id} ({total} in all); showing "
                f"{offset + 1}-{offset + len(page)}, soonest due first"
                + (f". {more} more: call again with offset={offset + len(page)}." if more > 0 else "."))
        return ToolResult(ok=True, data=page, text=head + "\n" + "\n".join(step_line(r) for r in page))

    return ToolSpec(
        name="list_quest_workflows",
        handler=handler,
        description=("List the steps of the workflows on a quest: step id, title, who it belongs to "
                     "(a name), its window, its status (pending, open, done, late or missed) and "
                     "when it is due. Filter by person, status or words; one page at a time."),
        when_to_use=("You need what a workflow still asks of someone: the approvals or steps waiting "
                     "on a person, what is late or due soon, or a step found by its words. Read it "
                     "BEFORE writing any message that says what is waiting."),
        when_not_to_use=("Asks raised for people (list_open_decisions_for_quest), goals "
                         "(list_quest_goals), or changing a workflow."),
        parameters={"type": "object", "properties": {
            "quest_id": QUEST_ID_PARAM,
            "assignee": {"type": "string",
                         "description": "Only steps for this person: part of their name, or their user id."},
            "open_only": {"type": "boolean",
                          "description": "Hide done steps (default true). Pending and not-yet-started steps stay."},
            "status": {"type": "string",
                       "description": "Only these statuses, comma separated (pending, open, late, missed, done)."},
            "text_contains": {"type": "string",
                              "description": "Only steps whose id, title or instructions contain this "
                                             "(case-insensitive), for example approve_."},
            "limit": {"type": "integer",
                      "description": f"Page size (default {WORKFLOW_STEPS_DEFAULT_LIMIT}, at most "
                                     f"{WORKFLOW_STEPS_MAX_LIMIT})."},
            "offset": {"type": "integer", "description": "Skip this many matches (next page)."}}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("workflow", "workflows", "steps", "step", "approval", "approvals", "waiting", "late",
                  "missed", "pending", "due", "assigned", "window", "schedule", "cadence"),
        context_args={"quest_id": "quest_id"})


def resolve_member(who: Any, members: List[Mapping[str, Any]]) -> "tuple[Optional[str], Optional[str]]":
    """A quest member from a user id, a name or part of one, or an email: ``(user_id, None)``, or
    ``(None, why)`` naming the people it could have been so the call can be fixed."""
    want = str(who or "").strip().lower()
    if not want:
        return None, "a human step needs an assignee"
    known = [m for m in members if m.get("user_id")]
    exact = [m for m in known if want in (str(m["user_id"]).lower(), str(m.get("email") or "").lower())]
    hits = exact or [m for m in known if want in str(m.get("name") or "").lower()
                     or want == str(m.get("email") or "").split("@")[0].lower()]
    if len(hits) == 1:
        return str(hits[0]["user_id"]), None
    roster = ", ".join(str(m.get("name") or m["user_id"]) for m in known) or "nobody"
    return None, (f"{who!r} matches {len(hits)} people on this quest. Members: {roster}.")


def workflow_step_body(index: int, raw: Mapping[str, Any], members: List[Mapping[str, Any]]) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """One step in the wire shape. A human step (``assignee``) has a window (``starts_at`` to
    ``ends_at``); an AI step (``rep_id``) runs at ``run_at``. Either may repeat."""
    title = str(raw.get("title") or "").strip()
    label = f"step {index + 1}"
    if not title:
        return None, f"{label} needs a title"
    repeat = None
    if raw.get("repeat"):
        repeat = {"freq": str(raw["repeat"]), "interval": 1}
        if raw.get("by_weekday") is not None:
            repeat["byWeekday"] = list(raw["by_weekday"])
        if raw.get("tz"):
            repeat["tz"] = str(raw["tz"])
    if raw.get("rep_id"):
        if not raw.get("run_at"):
            return None, f"{label} ({title!r}) is an AI step and needs run_at"
        actor = {"kind": "ai", "repId": str(raw["rep_id"])}
        timing: Dict[str, Any] = {"kind": "at", "runAt": str(raw["run_at"]), "repeat": repeat}
    else:
        user_id, why = resolve_member(raw.get("assignee"), members)
        if why:
            return None, f"{label} ({title!r}): {why}"
        if not (raw.get("starts_at") and raw.get("ends_at")):
            return None, f"{label} ({title!r}) is a person's step and needs starts_at and ends_at"
        actor = {"kind": "human", "userId": user_id}
        timing = {"kind": "window", "startsAt": str(raw["starts_at"]), "endsAt": str(raw["ends_at"]),
                  "repeat": repeat}
    return {"stepId": f"s{index + 1}", "title": title, "actor": actor,
            "instructions": str(raw.get("instructions") or ""), "timing": timing}, None


def create_quest_workflow_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = quest_id_of(args, ctx)
        if not quest_id:
            return NO_QUEST
        title = str(args.get("title") or "").strip()
        raw_steps = args.get("steps")
        if not title or not isinstance(raw_steps, list) or not raw_steps:
            return ToolResult(ok=False, text="A workflow needs a title and at least one step.")
        client = factory()
        members = client.list_assignable_members(quest_id) or []
        steps: List[Dict[str, Any]] = []
        for i, raw in enumerate(raw_steps):
            body, why = workflow_step_body(i, raw if isinstance(raw, dict) else {}, members)
            if why:
                return ToolResult(ok=False, text=f"Nothing was created: {why}.")
            steps.append(body)
        edges = ([{"fromStepId": a["stepId"], "toStepId": b["stepId"]} for a, b in zip(steps, steps[1:])]
                 if args.get("chain") else [])
        try:
            made = client.create_quest_workflow(quest_id, title, steps, edges=edges,
                                                status=str(args.get("status") or "") or None)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        ident = (made or {}).get("workflowId")
        if not ident:
            return ToolResult(ok=False, text="Nothing was created: Quest returned no workflow.")
        return ToolResult(ok=True, data=made, text=(
            f"Created the workflow {title!r} ({ident}) on quest {quest_id} with {len(steps)} steps. "
            f"It is on the Workflows page for the quest's members."))

    return ToolSpec(
        name="create_quest_workflow",
        handler=handler,
        description=("Create a workflow on a quest, the entry that appears on the Workflows page: a "
                     "title and ordered steps, each for a named person (a human step with a window) or "
                     "an AI rep (an AI step that runs at a time), optionally repeating daily or weekly."),
        when_to_use=("A person asks for a workflow, routine or repeating process on a quest. Name people "
                     "as they are named on the quest (a first name is enough); read it back afterwards with "
                     "list_quest_workflows."),
        when_not_to_use=("A goal, habit or one-off task (create_quest_goal, create_task). Changing or "
                         "deleting a workflow that exists."),
        parameters={"type": "object", "required": ["title", "steps"], "properties": {
            "quest_id": QUEST_ID_PARAM,
            "title": {"type": "string", "description": "The workflow's name."},
            "status": {"type": "string", "enum": ["draft"],
                       "description": "draft keeps it from notifying anyone until someone sets it live; "
                                      "leave out to create it live."},
            "chain": {"type": "boolean",
                      "description": "Draw an arrow from each step to the next, in the order given."},
            "steps": {"type": "array", "description": (
                "Ordered steps. A person's step: title, assignee (a name, email or user id of a quest "
                "member), starts_at and ends_at (ISO date-times with a zone). An AI step: title, rep_id "
                "and run_at. Both may carry instructions, repeat (daily, weekly or monthly), by_weekday "
                "(0 Monday to 6 Sunday, weekly only) and tz (IANA zone)."), "items": {
                    "type": "object", "required": ["title"], "properties": {
                        "title": {"type": "string"}, "instructions": {"type": "string"},
                        "assignee": {"type": "string"}, "rep_id": {"type": "string"},
                        "starts_at": {"type": "string"}, "ends_at": {"type": "string"},
                        "run_at": {"type": "string"}, "repeat": {"type": "string"},
                        "by_weekday": {"type": "array", "items": {"type": "integer"}},
                        "tz": {"type": "string"}}}}}},
        mutates=True, workflow_step=False, origin="standard",
        keywords=("workflow", "workflows", "create", "routine", "process", "steps", "repeat", "daily",
                  "weekly", "assistant", "schedule", "cadence"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=60.0)


def find_quest_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        client = factory()
        wanted = words_of(args.get("name"))
        rows: List[Dict[str, Any]] = []
        for team in client.list_teams():
            team_id = team.get("team_id") or team.get("id")
            if team_id:
                rows.extend(client.list_quests(team_id=team_id))
        matches = [r for r in rows if not wanted or wanted <= words_of(r.get("outcome"))]
        if not matches:
            return ToolResult(ok=False, data=[], text=(
                f"No quest you can reach matches {str(args.get('name') or '')!r} ({len(rows)} quests checked). "
                f"Try fewer words, or tell the person you cannot see that quest."))
        shown = matches[:15]
        lines = [f"- {r.get('quest_id') or r.get('id')}{' (completed)' if r.get('completed') else ''}: "
                 f"{trim(r.get('outcome'), 100)}" for r in shown]
        more = f"\n{len(matches) - len(shown)} more: add words to narrow it." if len(matches) > len(shown) else ""
        return ToolResult(ok=True, data=shown, text="Quests you can reach (pass the id as quest_id):\n"
                                                      + "\n".join(lines) + more)

    return ToolSpec(
        name="find_quest",
        handler=handler,
        description=("Find a quest's id from words in its name or outcome, across every team you can reach. "
                     "Returns quest ids to pass as quest_id to the other quest tools."),
        when_to_use=("A quest tool said no quest is open in this run, or the person named a quest and you "
                     "do not have its id. Call this once, then use the id."),
        when_not_to_use="The run already is on the quest (its tools default to it).",
        parameters={"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Words from the quest's name or outcome, for example '1,000 Subscribers'. "
                                    "Empty lists the first quests."}}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("quest", "find", "which", "open", "id", "name", "locate", "subscribers"))


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
    "list_quest_goals": with_credentials(list_quest_goals_spec),
    "list_quest_workflows": with_credentials(list_quest_workflows_spec),
    "create_quest_workflow": with_credentials(create_quest_workflow_spec),
    "find_quest": with_credentials(find_quest_spec),
    "create_goal": with_credentials(create_goal_spec),
    "update_goal": with_credentials(update_goal_spec),
}
STANDARD_TOOL_BUILDERS.update(QUEST_OPERATION_BUILDERS)

#: Operations every autopilot batch is pinned to (see ``runner.run_operations``): the asks the pass
#: works first and the progress it owes on every pass.
AUTOPILOT_PINS = ("list_asks", "record_ask", "update_ask", "create_decision", "update_outcome_progress")
