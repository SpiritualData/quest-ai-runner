"""Two dated values for one fact: the latest wins, resolved in code.

A record gives a reading with a date ("onboarding completion 50.0%, 5 Nov"); an older record gives the same fact at another date and
another value. The writer used to be handed both and mixed them. Here the readings are lined up by code: two lines of the evidence that
each state one date and some figures, and that share most of their subject words, are two readings of one fact when their dates and
their figures differ. The one with the latest date is the fact; the others are superseded. The writer is told only the resolved facts
and their dates, and a draft that states a superseded figure or date, or two different dates for one thing, is a measurement failure
the repair rounds fix (see ``reply_standards.measure``).

Nothing here reads the wording of a model output (hard rule #3): the inputs are the records the turn held, and the comparison is over
their dates, figures and shared subject words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import FrozenSet, List, Optional, Tuple

from hubofhubs.api import DAY_THEN_MONTH, MONTHS, MONTH_NUMBER, MONTH_THEN_DAY, dates_in
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import NUMBER, SENTENCE_END, Violation

WORD = re.compile(r"[a-z]{3,}")
STOP = frozenset("the and for with from that this was were are has have had not but its our your their per out new all one two as of on at by".split())
SAME_FACT = 0.5          # shared subject words (of the smaller set) at which two readings are of one fact
SAME_CLAUSE = 0.6        # the same, for two clauses of one draft
MAX_LINE = 300
MAX_READINGS = 400


def day_of(day_month: Tuple[int, int], today: date) -> Optional[date]:
    """A (day, month) with no year, placed in the latest year that is not after today (a date a month ahead is still this year's)."""
    try:
        placed = date(today.year, day_month[1], day_month[0])
    except ValueError:
        return None
    return placed if placed <= today + timedelta(days=45) else date(today.year - 1, day_month[1], day_month[0])


def subject_words(text: str) -> FrozenSet[str]:
    months = set(MONTHS.split("|"))
    return frozenset(w for w in WORD.findall((text or "").lower()) if w not in STOP and w[:3] not in months)


def figures_of(text: str) -> FrozenSet[str]:
    return frozenset(m.group(0).replace(",", "") for m in NUMBER.finditer(text or ""))


def strip_dates(text: str) -> str:
    for stated, _ in dates_in(text):
        text = text.replace(stated, " ")
    return text


def similar(a: FrozenSet[str], b: FrozenSet[str], bar: float) -> bool:
    """Two word sets are about one subject: they share at least two words and the shared part is ``bar`` of the smaller set."""
    return len(a & b) >= 2 and len(a & b) / min(len(a), len(b)) >= bar


@dataclass
class Reading:
    line: str          # the stretch of the record around the figure
    day: date
    stated: str        # the date as the record wrote it
    words: FrozenSet[str]
    figures: FrozenSet[str]


WINDOW = 90            # characters either side of a figure that belong to its reading
MEASURE = re.compile(r"(?<![\w.])(?P<n>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?%|(?P<sym>[$€£])\s?(?P<m>\d{1,3}(?:,\d{3})*(?:\.\d+)?)")


def date_spans(line: str, today: date) -> List[Tuple[int, int, date, str]]:
    found = []
    taken: List[Tuple[int, int]] = []
    for rx in (DAY_THEN_MONTH, MONTH_THEN_DAY):
        for m in rx.finditer(line):
            month = MONTH_NUMBER.get(m.group("mon").lower()[:3])
            if month is None or any(a < m.end() and m.start() < b for a, b in taken):
                continue
            placed = day_of((int(m.group("d1")), month), today)
            if placed:
                taken.append((m.start(), m.end()))
                found.append((m.start(), m.end(), placed, m.group(0).strip(" .")))
    return found


def readings_in(evidence: str, today: date) -> List[Reading]:
    """Every measured figure (a percentage or an amount) in the records, with the date nearest to it in the same line and the words around it.
    A record line may hold several readings (before and after, two dates): each figure is read with its own nearest date."""
    out: List[Reading] = []
    for raw in (evidence or "").splitlines():
        line = raw.strip(" \t-*•|#>")
        if not 12 <= len(line) <= 600:
            continue
        spans = date_spans(line, today)
        if not spans:
            continue
        for m in MEASURE.finditer(line):
            centre = (m.start() + m.end()) // 2
            start, end, placed, stated = min(spans, key=lambda sp: min(abs(sp[0] - centre), abs(sp[1] - centre)))
            if min(abs(start - centre), abs(end - centre)) > 2 * WINDOW:
                continue
            lo, hi = max(0, m.start() - WINDOW), min(len(line), m.end() + WINDOW)
            window = line[lo:hi]
            rest = strip_dates(MEASURE.sub(" ", window))
            figure = (m.group("n") or m.group("m")).replace(",", "")
            out.append(Reading(window.strip(), placed, stated, subject_words(rest), frozenset({figure})))
            if len(out) >= MAX_READINGS:
                return out
    return out


@dataclass
class Resolved:
    """The facts that had more than one dated reading: what is current, and what the older readings said."""
    current: List[Reading] = field(default_factory=list)
    stale_figures: FrozenSet[str] = frozenset()
    stale_days: FrozenSet[Tuple[int, int]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.current)

    def block(self) -> str:
        """What the writer is told: only the resolved facts and their dates."""
        if not self.current:
            return ""
        lines = "\n".join(f"- {r.line[:200]} (as of {r.stated})" for r in self.current[:8])
        return ("--- FACTS WITH MORE THAN ONE DATED READING (resolved by code) ---\n"
                "Your records give these facts at different dates. The reading below is the latest. Use it, write \"as of <date>\" beside "
                "it, and do not state an older reading, figure or date as current:\n" + lines + "\n")

    def violations(self, draft: str) -> List[Violation]:
        out: List[Violation] = []
        text = strip_dates(draft or "")
        said = sorted(f for f in figures_of(text) if f in self.stale_figures)
        said_days = [stated for stated, parts in dates_in(draft or "") if any(p in self.stale_days for p in parts)]
        if said or said_days:
            latest = self.current[0]
            old = ", ".join(f"'{v}'" for v in (said + said_days)[:4])
            out.append(Violation(
                "superseded", f"{old} is an older reading of a fact your records hold at a later date; state the latest "
                f"({latest.line[:120]!r}, as of {latest.stated}) and say \"as of <date>\", never both as current", tuple(said + said_days)))
        out.extend(self.contradictions(draft))
        return out

    @staticmethod
    def contradictions(draft: str) -> List[Violation]:
        """Two clauses of one reply that state different dates for the same subject (compared by shared subject words and date parts)."""
        clauses = [c for s in SENTENCE_END.split(draft or "") for c in re.split(r"(?<=[,;:])\s+", s) if c.strip()]
        dated = []
        for c in clauses:
            ds = dates_in(c)
            if len(ds) == 1:
                dated.append((c, frozenset(ds[0][1]), subject_words(strip_dates(c))))
        out: List[Violation] = []
        for i in range(len(dated)):
            for j in range(i + 1, len(dated)):
                (a, da, wa), (b, db, wb) = dated[i], dated[j]
                if da != db and similar(wa, wb, SAME_CLAUSE):
                    out.append(Violation(
                        "contradiction", f"it states two different dates for one thing ({a.strip()[:60]!r} and {b.strip()[:60]!r}); "
                        "state that one thing once, with the date your records hold latest"))
                    return out
        return out


def resolve(evidence: str, today: Optional[date] = None) -> Resolved:
    """Line up the dated readings in ``evidence`` and resolve each fact that has more than one by date. Never raises."""
    try:
        today = today or date.today()
        items = readings_in(evidence, today)
        parent = list(range(len(items)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a.day != b.day and a.figures != b.figures and similar(a.words, b.words, SAME_FACT):
                    parent[find(i)] = find(j)
        groups: dict = {}
        for i in range(len(items)):
            groups.setdefault(find(i), []).append(items[i])
        current: List[Reading] = []
        stale_figures: set = set()
        stale_days: set = set()
        held_elsewhere = set()
        for members in groups.values():
            if len({m.day for m in members}) < 2:
                continue
            latest = max(members, key=lambda m: m.day)
            current.append(latest)
            for m in members:
                if m.day < latest.day:
                    stale_figures |= (m.figures - latest.figures)
                    for _, parts in dates_in(m.line):
                        stale_days |= set(parts)
                else:
                    held_elsewhere |= set(m.figures)
                    for _, parts in dates_in(m.line):
                        held_elsewhere |= set(parts)
        stale_days -= {p for r in current for _, parts in dates_in(r.line) for p in parts}
        return Resolved(current=current, stale_figures=frozenset(stale_figures - held_elsewhere), stale_days=frozenset(stale_days))
    except Exception:  # noqa: BLE001 -- resolving must never break a turn
        return Resolved()
