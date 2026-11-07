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
NO_STATUS = "I am starting on this now."
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


IDENTIFIER = re.compile(r"\b(?:[a-z]+_[0-9a-f]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{12,}|\d{4}-\d{2}-\d{2}T[\d:.+Z-]+)\b", re.IGNORECASE)
WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


def plain(text: str) -> str:
    """``text`` for a reader, or "" when it carries a record id or a timestamp (a raw fact is not a sentence to show)."""
    text = " ".join((text or "").split())
    return "" if IDENTIFIER.search(text) else text


def scrub(text: str) -> str:
    """``text`` with record ids and timestamps taken out, so a backend's own words can be shown without them."""
    return " ".join(IDENTIFIER.sub("", text or "").split())


def humanize_tool(name: str) -> str:
    words = [w for w in str(name).split("_") if w]
    return " ".join(words[1:] if words and words[0] in ("list", "read", "get", "find", "search", "show") and len(words) > 1 else words)


def read_summary(gathered: Iterable[Any]) -> str:
    """A plain line for what the turn has looked at so far (the read tools it ran), or "" when it has read nothing: "I looked at the quest workflows."."""
    names: List[str] = []
    for entry in gathered:
        locator = str(entry.get("locator") or "") if isinstance(entry, Mapping) else ""
        if locator.startswith("tool:") and entry.get("tool_status") not in ("refused",) and not entry.get("by_code"):
            name = humanize_tool(locator[5:])
            if name and name not in names:
                names.append(name)
    return ("I looked at " + ", ".join(names[:3]) + ".") if names else ""


def planned_reads(plan: Any) -> str:
    """A plain line for the read tools a plan is about to run ("I am looking at quest workflows first."); "" when it plans none by name."""
    names: List[str] = []
    for call in getattr(plan, "tool_calls", None) or []:
        name = humanize_tool(str((call or {}).get("name") or "")) if isinstance(call, Mapping) else ""
        if name and name not in names:
            names.append(name)
    return ("I am looking at " + ", ".join(names[:3]) + " first.") if names else ""


def schedule_phrase(data: Mapping[str, Any]) -> str:
    """How often a recorded routine repeats, from its steps' own timing, in the words the workflow listing uses ("weekly on Thursday"); "" when it does not repeat."""
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import repeat_words
    for step in data.get("steps") or []:
        repeat = ((step.get("timing") or {}).get("repeat")) if isinstance(step, Mapping) else None
        if repeat and repeat.get("freq"):
            return repeat_words(repeat)
    return ""


def next_occurrence(data: Mapping[str, Any]) -> str:
    """The date of the recorded routine's current or next occurrence ("Thursday 12 November"), from its structured step (its window or timing start, as written, no zone
    conversion); "" when the step carries none."""
    import datetime
    for step in data.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        raw = ((step.get("window") or {}).get("startsAt")) or ((step.get("timing") or {}).get("startsAt"))
        try:
            day = datetime.datetime.fromisoformat(str(raw)).date()
        except (TypeError, ValueError):
            continue
        return f"{WEEKDAY_ENGLISH[day.weekday()]} {day.day} {MONTH_ENGLISH[day.month - 1]}"
    return ""


WEEKDAY_ENGLISH = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_ENGLISH = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def already_phrase(entry: Mapping[str, Any]) -> str:
    """One plain sentence for a write that found everything already there: "It's already set: <title>, weekly on Thursdays." Built from the tool's own data (title, schedule);
    never the tool's text, and no ids."""
    data = entry.get("tool_data") if isinstance(entry.get("tool_data"), Mapping) else {}
    goal = data.get("goal") if isinstance(data.get("goal"), Mapping) else {}
    title = plain(str(data.get("title") or goal.get("name") or ""))
    when = schedule_phrase(data)
    if not title:
        return "It's already set."
    upcoming = next_occurrence(data)
    return f"It's already set: {title}" + (f", {when}" if when else "") + (f" (next: {upcoming})" if upcoming else "") + "."


def status_texts(reading: Optional[MetricReading], facts: Sequence[str], doing: str, *, evidence: str, voice: bool = False, found: str = "") -> dict:
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
    status = " ".join(parts) or found or NO_STATUS
    nxt = plain(grounded_line(doing, evidence)).rstrip(".") if doing != "Writing the versions now and keeping them on the quest." else doing.rstrip(".")
    if "?" in nxt:
        nxt = ""         # message 1 is a status: a question is never what is being done
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


CLEAN_VALUE_CHARS = 48
LEADING_FIGURE = re.compile(r"^\s*(\d+(?:[.,]\d+)?\s*(?:%|percent\b|of\s+\d+(?:[.,]\d+)?\b)?)")
YES_NO = re.compile(r"^\s*(yes|no)\b", re.IGNORECASE)


def clean_value(value: str) -> str:
    """The recorded value as one short figure or phrase, never a piece of the record's sentence: a short record is itself; a longer one gives its leading figure ("0 of 6", "50.0 percent")
    or its leading Yes or No; anything else gives "" (a reader sees a clean value or none)."""
    raw = " ".join(str(value or "").split()).removeprefix("still ").strip()
    if not raw or raw == NOT_RECORDED:
        return ""
    if len(raw) <= CLEAN_VALUE_CHARS and raw.count("(") == raw.count(")"):
        return short_value(raw)
    for pattern in (LEADING_FIGURE, YES_NO):
        found = pattern.match(raw)
        if found:
            return found.group(1).strip()
    return ""


def closing_lines(before: Optional[MetricReading], after: Optional[MetricReading], *, reread: bool,
                  already: Sequence[str], voice: bool = False) -> str:
    """The code-written tail of message 2: ONE clean metric line, or none: the milestone's name and the value before and after ("unchanged: 0 of 6" or "was 3, now 5"), each a short value
    from the record (``clean_value``). No line when the record holds no value that can be shown cleanly. Then what was already in place."""
    lines: List[str] = []
    if before is not None and after is not None:
        was, now = clean_value(before.value), clean_value(after.value)
        label = (before.milestone or before.done_when or "")[:70].strip()
        if was and now and label:
            lines.append(f"Metric ({label}): unchanged: {was}." if now == was else f"Metric ({label}): was {was}, now {now}.")
    if already and not voice:
        lines.append(" ".join(already[:2]))
    return "\n".join(lines)
