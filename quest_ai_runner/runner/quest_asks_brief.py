"""quest_asks_brief -- the "What You Asked" section of an autopilot batch.

The quest's asks ledger (``GET /api/asks``) holds what people asked of the AI on this quest, and
the default autopilot instructions tell a run to work the open ones first. This module turns that
ledger, plus the asks already waiting on a person (decision-requests), into the compact, budgeted
text the batch carries, following ``quest_experiments`` as the precedent: read once per quest,
``None`` for an unreadable list so a failed read renders as no section rather than "nothing was
asked", and the section carries its own short instruction so it rides in whole or not at all.

Which rows count is the ledger's own vocabulary (``feedback_ledger.OPEN_STATES``), minus
``awaiting acceptance``: a run that has already claimed an ask done is not owed more work on it.
A standing rule that is in force is not open but is shown, as a rule to follow. A row whose kind
is still ``unknown`` has never been sorted, and is marked so the run sorts it with ``update_ask``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .autopilot_periods import _parse_dt
from .feedback_ledger import (AWAITING_ACCEPTANCE, AWAITING_ANSWER, BLOCKED, IN_FORCE,
                              IN_PROGRESS, KIND_STANDING, KIND_UNKNOWN, OPEN_STATES)

log = logging.getLogger("quest-ai-runner.quest_asks_brief")

#: Characters the section may use (about 900 tokens).
ASKS_BRIEF_MAX_CHARS = 3600
#: How many rows of each list are shown before "+N more".
MAX_OPEN_ASKS = 12
MAX_RULES = 6
MAX_WAITING = 8
#: Characters of a person's words shown per ask.
ASK_TEXT_LIMIT = 260
#: What a person said back, and what it answers. The answer is the most valuable line in the
#: section (it is what the last run was waiting for), so it gets its own, larger budget, spent
#: before the run's own notes; once a budget is used up, later asks show no more of that kind.
ANSWER_LIMIT = 320
QUESTION_LIMIT = 160
RUN_NOTE_LIMIT = 160
ANSWERS_BUDGET = 1500
RUN_NOTES_BUDGET = 600
#: Below this many characters left in a budget a note is dropped rather than cut to a stub.
MIN_NOTE_ROOM = 60

HEADER = ("WHAT YOU ASKED. What people asked of you on this quest, and what is already waiting on "
          "them. Work the open asks first; the key in front of each is what update_ask needs.")


def fetch_asks(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """The quest's ask rows, or ``None`` when they could not be read. Never raises."""
    lister = getattr(client, "list_asks", None)
    if not callable(lister):
        return None
    try:
        rows = lister(quest_id=quest_id, limit=200)
        return None if rows is None else [r for r in rows if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 -- additive context; never block a pass
        log.info("could not read asks for quest %s (%s: %s)", quest_id, type(e).__name__, e)
        return None


def fetch_open_decisions(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """The asks already waiting on a person, or ``None`` when they could not be read."""
    lister = getattr(client, "list_open_decisions_for_quest", None)
    if not callable(lister):
        return None
    try:
        return [r for r in (lister(quest_id) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001
        log.info("could not read open decisions for quest %s (%s: %s)", quest_id, type(e).__name__, e)
        return None


def one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def age_label(row: Dict[str, Any], now: datetime) -> str:
    when = _parse_dt(row.get("occurred_at") or row.get("created_at"))
    if when is None:
        return ""
    days = max(0, (now - when).days)
    return "today" if days == 0 else f"{days}d ago"


def is_owed(row: Dict[str, Any]) -> bool:
    state = str(row.get("state") or "open")
    return state in OPEN_STATES and state != AWAITING_ACCEPTANCE


def ask_entry(row: Dict[str, Any], now: datetime) -> str:
    kind = str(row.get("kind") or KIND_UNKNOWN)
    label = f"{row.get('state') or 'open'}, " + ("NOT SORTED" if kind == KIND_UNKNOWN else kind)
    who = ", ".join(p for p in (str(row.get("author") or ""), age_label(row, now)) if p)
    return (f"- {row.get('key')} [{label}]" + (f" {who}:" if who else "") + " "
            + one_line(row.get("text"), ASK_TEXT_LIMIT))


def history_of(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [h for h in (row.get("history") or []) if isinstance(h, dict)]


def last_person_note(row: Dict[str, Any]) -> Optional[int]:
    """Index in the ask's history of the latest entry a PERSON wrote words on, or ``None``."""
    entries = history_of(row)
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].get("by") == "person" and str(entries[index].get("note") or "").strip():
            return index
    return None


def question_before(row: Dict[str, Any], index: int) -> str:
    """The question a person's entry at ``index`` answers: the run's entry just before it, when
    that entry was an awaiting-answer one. Empty when the person was not answering a question."""
    entries = history_of(row)
    for earlier in range(index - 1, -1, -1):
        if entries[earlier].get("by") != "person":
            if entries[earlier].get("state") == AWAITING_ANSWER:
                return str(entries[earlier].get("note") or "")
            return ""
    return ""


def last_run_note(row: Dict[str, Any]) -> str:
    for entry in reversed(history_of(row)):
        if entry.get("by") != "person" and str(entry.get("note") or "").strip():
            return str(entry["note"])
    return str(row.get("last_note") or "")


def spend(budget: List[int], text: str, limit: int) -> str:
    """``text`` cut to ``limit`` and to what is left of ``budget`` (a one-item list, decremented),
    or ``""`` when too little is left to be worth showing."""
    room = min(limit, budget[0])
    if room < MIN_NOTE_ROOM:
        return ""
    shown = one_line(text, room)
    budget[0] -= len(shown)
    return shown


def note_lines(row: Dict[str, Any], answers: List[int], run_notes: List[int]) -> List[str]:
    """The indented lines under an open ask: what the person said back (and the question it
    answered), the question still waiting on them, and the run's own last note when it is mid-way."""
    lines: List[str] = []
    state = str(row.get("state") or "open")
    person = last_person_note(row)
    if person is not None:
        said = spend(answers, history_of(row)[person]["note"], ANSWER_LIMIT)
        asked = question_before(row, person)
        if said and asked:
            question = one_line(asked, QUESTION_LIMIT)
            lines += [f"    You asked: {question}", f"    Their answer: {said}"]
        elif said:
            lines.append(f"    Their note: {said}")
    elif state == AWAITING_ANSWER and str(row.get("last_note") or "").strip():
        lines.append(f"    You asked: {one_line(row.get('last_note'), QUESTION_LIMIT)} "
                     f"(still waiting on their answer)")
    if state in (IN_PROGRESS, BLOCKED):
        note = spend(run_notes, last_run_note(row), RUN_NOTE_LIMIT)
        if note:
            lines.append(f"    Your last note: {note}")
    return lines


def rule_entry(row: Dict[str, Any]) -> str:
    return f"- {row.get('key')} {one_line(row.get('text'), ASK_TEXT_LIMIT)}"


def waiting_entry(row: Dict[str, Any]) -> str:
    return f"- [{row.get('kind') or 'approve'}] {one_line(row.get('summary'), 140)}"


def capped(lines: Iterable[str], limit: int) -> List[str]:
    rows = list(lines)
    return rows[:limit] + ([f"- +{len(rows) - limit} more"] if len(rows) > limit else [])


def asks_brief_section(asks: Optional[List[Dict[str, Any]]],
                       decisions: Optional[List[Dict[str, Any]]] = None, *,
                       now: Optional[datetime] = None,
                       max_chars: int = ASKS_BRIEF_MAX_CHARS) -> str:
    """The section text, or ``""`` when the asks could not be read or there is nothing to say.

    ``decisions`` (``None`` = unreadable) adds what is already waiting on a person, so a run does
    not raise it twice; it never makes a section on its own, since the asks are the point.
    """
    if asks is None:
        return ""
    when = now or datetime.now(timezone.utc)
    owed = sorted((r for r in asks if is_owed(r)),
                  key=lambda r: str(r.get("occurred_at") or r.get("first_seen_at") or ""))
    rules = [r for r in asks if r.get("kind") == KIND_STANDING and r.get("state") == IN_FORCE]
    if not owed and not rules:
        return ""
    lines = [HEADER]
    if owed:
        answers, run_notes = [ANSWERS_BUDGET], [RUN_NOTES_BUDGET]
        shown = owed[:MAX_OPEN_ASKS]
        entries = ["\n".join([ask_entry(r, when)] + note_lines(r, answers, run_notes)) for r in shown]
        if len(owed) > MAX_OPEN_ASKS:
            entries.append(f"- +{len(owed) - MAX_OPEN_ASKS} more")
        lines += ["", "Open asks:"] + entries
        if any(str(r.get("state") or "") == AWAITING_ANSWER for r in owed):
            lines.append("Awaiting answer means your question is open in the person's Asks for You: "
                         "do not work that ask or ask again until it is answered.")
        if any(last_person_note(r) is not None for r in shown):
            lines.append("Their answer or note is the latest word on that ask: act on it first.")
        if any(str(r.get("kind") or KIND_UNKNOWN) == KIND_UNKNOWN for r in owed):
            lines.append("NOT SORTED means nobody has said whether it is a request, a standing "
                         "rule, background or a question: sort it with update_ask (kind).")
    if rules:
        lines += ["", "Standing rules in force (follow them in everything you do):"]
        lines += capped((rule_entry(r) for r in rules), MAX_RULES)
    if decisions:
        lines += ["", "Already waiting on a person (do not raise these again):"]
        lines += capped((waiting_entry(r) for r in decisions), MAX_WAITING)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars - 3].rstrip() + "..."
