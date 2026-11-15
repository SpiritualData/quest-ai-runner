"""Resolve a relative date reference ("yesterday", "last Thursday", "this week") to the files whose names carry that date.

People point at a file by when it happened, and many folders name files by date (``2026-11-05-office-hour-...``). Matching the
name is a pure function of the reference and today's date, so the code does it; the model is not asked to do calendar maths.
Only the person's own words and the planner's structured ``date`` argument are read here, never the words of a reply.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import List, Optional, Sequence, Tuple

DateSpan = Tuple[date, date]
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NAME_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
ISO_SPAN = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to)\s*(\d{4}-\d{2}-\d{2})\s*$")
ISO_ONE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")
AGO = re.compile(r"\b(\d{1,3}|a|one|two|three|four|five|six|seven)\s+(day|days|week|weeks)\s+ago\b")
WORD_NUMBER = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
MAX_SPAN_DAYS = 31


def week_of(day: date) -> DateSpan:
    """Monday to Sunday of the week holding ``day``."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def last_weekday(today: date, weekday: int) -> date:
    """The most recent given weekday strictly before ``today``."""
    back = (today.weekday() - weekday) % 7
    return today - timedelta(days=back or 7)


def resolve_date_reference(text: str, today: date) -> Optional[DateSpan]:
    """The inclusive date span one reference names, or None. Accepts an ISO date, ``A..B``, ``yesterday``, ``today``,
    ``last <weekday>``, ``this <weekday>`` or a bare weekday (the most recent one up to today), ``N days ago``,
    ``this week``, ``last week``, ``this month``, ``last month`` and ``recent`` (the last seven days)."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return None
    span = ISO_SPAN.match(low)
    if span:
        first, second = (date.fromisoformat(span.group(1)), date.fromisoformat(span.group(2)))
        return (first, second) if first <= second else (second, first)
    one = ISO_ONE.match(low)
    if one:
        day = date.fromisoformat(one.group(1))
        return day, day
    if re.search(r"\byesterday\b", low):
        day = today - timedelta(days=1)
        return day, day
    if re.search(r"\btoday\b", low):
        return today, today
    if re.search(r"\blast week\b", low):
        return week_of(today - timedelta(days=7))
    if re.search(r"\bthis week\b", low):
        return week_of(today)
    if re.search(r"\blast month\b", low):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if re.search(r"\bthis month\b", low):
        return today.replace(day=1), today
    if re.search(r"\b(recent|recently|lately|past few days)\b", low):
        return today - timedelta(days=7), today
    ago = AGO.search(low)
    if ago:
        count = WORD_NUMBER.get(ago.group(1)) or int(ago.group(1))
        days = count * (7 if ago.group(2).startswith("week") else 1)
        day = today - timedelta(days=days)
        return day, day
    for index, name in enumerate(WEEKDAYS):
        match = re.search(rf"\b(last|this|on)?\s*{name}\b", low)
        if match:
            if match.group(1) == "last" or today.weekday() != index and (today.weekday() - index) % 7 != 0:
                day = last_weekday(today, index)
            else:
                day = today
            return day, day
    return None


def date_references_in(text: str, today: date) -> List[Tuple[str, DateSpan]]:
    """Every relative or weekday date reference in a person's message, as (the phrase, its span). Used on the person's own words."""
    low = " ".join(str(text or "").lower().split())
    found: List[Tuple[str, DateSpan]] = []
    patterns = (r"\byesterday\b", r"\blast week\b", r"\bthis week\b", r"\blast month\b", r"\bthis month\b",
                r"\b\d{1,3}\s+(?:day|days|week|weeks)\s+ago\b", r"\b(?:last|this)\s+(?:" + "|".join(WEEKDAYS) + r")\b")
    for pattern in patterns:
        for match in re.finditer(pattern, low):
            span = resolve_date_reference(match.group(0), today)
            if span and all(match.group(0) != seen for seen, _ in found):
                found.append((match.group(0), span))
    return found


def name_date(name: str) -> Optional[date]:
    """The first ``YYYY-MM-DD`` in a file name, or None."""
    match = NAME_DATE.search(name or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def name_in_span(name: str, span: DateSpan) -> bool:
    day = name_date(name.rsplit("/", 1)[-1])
    return day is not None and span[0] <= day <= span[1]


def clamp_span(span: DateSpan) -> DateSpan:
    """A span never wider than ``MAX_SPAN_DAYS``, keeping its newest end."""
    if (span[1] - span[0]).days >= MAX_SPAN_DAYS:
        return span[1] - timedelta(days=MAX_SPAN_DAYS - 1), span[1]
    return span


def span_text(span: DateSpan) -> str:
    return span[0].isoformat() if span[0] == span[1] else f"{span[0].isoformat()}..{span[1].isoformat()}"


def newest_dated(paths: Sequence[str], today: date, per_folder: int = 3, folders: int = 6) -> List[Tuple[str, List[str], int]]:
    """For the folders holding date-named files up to ``today``: (folder, its newest file names, how many are dated), the folders
    with the newest files first. Names only; the caller renders them."""
    by_folder: dict = {}
    for path in paths:
        day = name_date(path.rsplit("/", 1)[-1])
        if day is not None and day <= today:
            by_folder.setdefault(path.rpartition("/")[0], []).append((day, path.rsplit("/", 1)[-1]))
    ranked = sorted(by_folder.items(), key=lambda item: max(item[1])[0], reverse=True)
    out: List[Tuple[str, List[str], int]] = []
    for folder, items in ranked:
        if len(items) < 3:
            continue
        items.sort(reverse=True)
        out.append((folder, [name for _, name in items[:per_folder]], len(items)))
        if len(out) >= folders:
            break
    return out
