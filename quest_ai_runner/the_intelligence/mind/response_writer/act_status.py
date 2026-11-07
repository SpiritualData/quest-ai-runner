"""Status first, then the work, then the result with its metric before and after: one more step of ``reply-in-steps``.

A request to ACT that will take work beyond one reply sends a first message (where things stand and what happens now), does the
work, and ends with a reply whose last lines are read by code: the same metric as the first message, read again, and anything
that was already in place. This module holds the pure parts (the decision, the metric reading, the message texts); the glue that
sends and appends is in ``orchestrator_turn_messages.py``.

Nothing here reads the wording of model output (hard rule #3). Whether to send comes from the planner's structured ``user_intent``
and the planned action (a deep escalation, or a tool step with a call that changes something). The figures come from the text of
records the turn read (goal lines, notes): ``Done when:`` and ``Current value:`` lines of stored data, not of anything a model wrote
this turn. A value that is not there is said as "not recorded yet", never filled in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from quest_ai_runner.the_intelligence.mind.response_writer.interim_policy import ungrounded_terms

NOT_RECORDED = "not recorded yet"
READ_AGAIN_FAILED = "could not be read again just now"
GENERIC_NEXT = "I will carry this out now and then check the result."
NO_STATUS = "I have not found a recorded status for this yet."
MAX_FACTS = 3

#: Fields of a record's text that end a ``Current value`` or ``Done when`` segment on the same line (goal lines hold them inline).
SEGMENT_END = r"(?=\s+(?:Done when|Current value|Metric|Source|Deadline|Owner|Milestone)\s*:|\s\|\s|\n|$)"
CURRENT_VALUE = re.compile(r"current value\s*:\s*(.*?)" + SEGMENT_END, re.IGNORECASE)
DONE_WHEN = re.compile(r"done when\s*:\s*(.*?)" + SEGMENT_END, re.IGNORECASE)
MILESTONE_LINE = re.compile(r"^\s*-\s+Milestone:\s*(?P<title>.*?)\s\|\s", re.IGNORECASE)
GOAL_LINE = re.compile(r"^\s*-\s+(?P<id>\S+)\s+\[[^\]]*\].*?\bfor [^:\n]+:\s*(?P<title>.*?)(?:\s\|\s|$)", re.IGNORECASE)
WORDS = re.compile(r"[a-z]{4,}")


@dataclass(frozen=True)
class MetricReading:
    """One milestone's measure as a record states it: ``value`` is ``NOT_RECORDED`` when the record has a Done-when but no figure."""
    record_id: str
    milestone: str
    done_when: str
    value: str
    call: Optional[Mapping[str, Any]] = None      # the read-only tool call that returned it (re-run for the "after" reading)

    @property
    def recorded(self) -> bool:
        return self.value != NOT_RECORDED


def format_figure(value: Any) -> str:
    """A reading as the reader sees it: a whole number without its decimal, a number to 4 places, nothing as ``NOT_RECORDED``."""
    if value is None or value == "":
        return NOT_RECORDED
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else f"{value:.4g}"
    return str(value)


def entry_blocks(entry: Any) -> List[str]:
    """The lines (or one block) of a gathered entry or plain text that may hold a record."""
    text = str(entry.get("text") or "") if isinstance(entry, Mapping) else str(entry or "")
    return [line for line in text.splitlines() if line.strip()]


def overlap(a: str, b: str) -> int:
    return len(set(WORDS.findall(a.lower())) & set(WORDS.findall(b.lower())))


def readings_in(entries: Iterable[Any]) -> List[MetricReading]:
    """Every record line of the entries that states a measure (a ``Done when`` or a ``Current value``)."""
    out: List[MetricReading] = []
    for entry in entries:
        call = entry.get("call") if isinstance(entry, Mapping) and isinstance(entry.get("call"), Mapping) else None
        for line in entry_blocks(entry):
            value = CURRENT_VALUE.search(line)
            done = DONE_WHEN.search(line)
            if not value and not done:
                continue
            goal = GOAL_LINE.match(line) or MILESTONE_LINE.match(line)
            figure = (value.group(1).strip().rstrip(".") if value else "")
            out.append(MetricReading(
                record_id=(goal.groupdict().get("id") or "") if goal else "", milestone=(goal.group("title").strip() if goal else ""),
                done_when=(done.group(1).strip().rstrip(".") if done else ""), value=figure or NOT_RECORDED, call=call))
    return out


def pick_reading(readings: Sequence[MetricReading], request: str) -> Optional[MetricReading]:
    """The reading the request is about: the one whose milestone shares most words with it (first when none do or only one exists)."""
    if not readings:
        return None
    return max(readings, key=lambda r: overlap(r.milestone + " " + r.done_when, request) * 1000 - readings.index(r))


def matching(readings: Sequence[MetricReading], before: MetricReading) -> Optional[MetricReading]:
    """The same record in a later read: by record id, else by milestone, else the only reading there is."""
    for r in readings:
        if before.record_id and r.record_id == before.record_id:
            return r
    for r in readings:
        if before.milestone and r.milestone == before.milestone:
            return r
    return readings[0] if len(readings) == 1 else None


def plans_work(*, action: str, mutating_calls: int) -> bool:
    """Work beyond one reply: a deep escalation, or a tool step with a call that changes something. Reads alone are not."""
    return action == "deep" or (action == "tool" and mutating_calls > 0)


def should_send_status(*, enabled: bool, user_turn: bool, user_intent: Optional[str], action: str, mutating_calls: int,
                       already_sent: bool) -> bool:
    """The structural decision for message 1: the planner read the message as an order (``act``), the plan is more than a read, once a turn."""
    return bool(enabled and user_turn and not already_sent and user_intent == "act"
                and plans_work(action=action, mutating_calls=mutating_calls))


def grounded_line(text: str, *allowed: str) -> str:
    """``text`` if every figure and name in it occurs in the stored text it was written from; otherwise empty."""
    text = " ".join((text or "").split())
    return text if text and not ungrounded_terms(text, "\n".join(allowed)) else ""


def status_texts(reading: Optional[MetricReading], facts: Sequence[str], doing: str, *, evidence: str, voice: bool = False) -> dict:
    """``{"status", "next", "text"}`` of message 1. The status is code-written from the reading and the planner's listed facts (each kept
    only when its figures and names are in ``evidence``); ``doing`` is kept only when grounded the same way, else the plain default."""
    parts: List[str] = []
    if reading is not None:
        if reading.milestone:
            parts.append(f"Milestone: {reading.milestone}.")
        if reading.done_when and not voice:
            parts.append(f"Done when: {reading.done_when}.")
        parts.append(f"Current value: {short_value(reading.value)}.")
    if not voice:
        kept = [grounded_line(f, evidence) for f in list(facts)[:MAX_FACTS]]
        parts.extend(f.rstrip(".") + "." for f in kept if f)
    status = " ".join(parts) or NO_STATUS
    nxt = grounded_line(doing, evidence).rstrip(".")
    nxt = (nxt + ".") if nxt else GENERIC_NEXT
    return {"status": status, "next": nxt, "text": f"Where this stands: {status}\n\nNow: {nxt}"}


VALUE_CHARS = 60


def short_value(value: str) -> str:
    """A recorded value as a reader wants it in one line. A record's text often starts with a log stamp ("<date and who>: <value>"); the stamp is dropped when what
    follows its first colon is the value, and the rest is cut at a sentence end or ``VALUE_CHARS``. Never the record's raw text."""
    text = " ".join(str(value or "").split())
    head, sep, rest = text.partition(": ")
    if sep and rest and len(head) <= 60 and any(c.isdigit() for c in head):
        text = rest
    if text.lower().startswith("still "):
        text = text[6:]          # "still No" says the value did not move; the closing line says that itself
    cut = re.search(r"[.;](\s|$)", text[:VALUE_CHARS])
    return (text[:cut.start()] if cut else text[:VALUE_CHARS]).strip() or NOT_RECORDED


def closing_lines(before: Optional[MetricReading], after: Optional[MetricReading], *, reread: bool,
                  already: Sequence[str], voice: bool = False) -> str:
    """The code-written tail of message 2: the metric (its name), the value before and after (the same record read again), then what was already in place."""
    lines: List[str] = []
    if before is not None:
        label = before.done_when or before.milestone or "the milestone's measure"
        was = short_value(before.value)
        if after is not None:
            now = short_value(after.value)
            lines.append(f"Metric ({label}): unchanged, still {was}." if now == was else f"Metric ({label}): was {was}, now {now}.")
        else:
            lines.append(f"Metric ({label}): was {was}; " + ("it could not be read again just now." if reread else "it was not read again this turn."))
    if already and not voice:
        lines.append("Already in place, so nothing was written again: " + "; ".join(already[:3]) + ".")
    return "\n".join(lines)
