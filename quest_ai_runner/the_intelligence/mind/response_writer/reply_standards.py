"""Situation standards for the reply: what a good reply looks like in each common situation, and the measurements that tell.

The reply writer is a cheap model and it makes the same few mistakes in the same few situations: a "where do I start" reply that
lists five steps and asks two questions, a date or a goal name that is in nothing the turn held, a "tell me in one breath" reply
that runs long. Improving the prompt alone leaves them recurring, so the answer checkpoint (the overseer's hook B) carries a
CHECK per situation: the standard for that situation, written once for a reviewer, and the deterministic measurements of the
draft that decide whether it was met.

Everything here is data plus counting:

* The SITUATION is selected from structured signals only: the planner's own ``reply_shape`` (and ``asks_first``), never from the
  words of any model output (hard rule #3).
* A MEASUREMENT is a count (questions, list lines, characters) or a comparison of a value the draft states (a date, a clock time, a
  quoted name) with the material the turn held, the way ``core/link_provenance_pass.py`` compares an address. The draft's wording
  decides nothing: a date the turn never held is a fact about the data, whatever sentence carries it.
* The HINT names the exact violation and the exact repair, built from the measurements, so a repair round needs no judgment.
* The last-resort REPAIR is the cheapest deterministic one that is safe: drop the sentence that states an unsupported value, keep
  only the first allowed questions. Anything that cannot be repaired safely ships as it is.

Plain integers are not judged (a suggested "10 minutes" is advice, not a claim about the person's records); dates, clock times and
quoted names are the values a reader acts on and the ones a cheap model invents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SHAPE_YES_NO = "yes_no"
SHAPE_ONE_STEP = "one_step"
SHAPE_ASK_FIRST = "ask_first"
SHAPE_CLARIFY = "clarify"
SHAPE_BRIEF = "brief"
SHAPE_READBACK = "readback"
SHAPE_DECLINE = "decline"
SHAPE_SUPPORT = "support"
SHAPE_DEFAULT = "default"
SHAPE_SPOKEN = "spoken"            # selected from the planner's ``delivery`` field, not offered as a ``reply_shape``
SHAPE_INFORMATION = "information"  # selected from the planner's ``user_intent`` (ask) on a default shape
REPLY_SHAPES = (SHAPE_YES_NO, SHAPE_ONE_STEP, SHAPE_ASK_FIRST, SHAPE_CLARIFY, SHAPE_BRIEF, SHAPE_READBACK, SHAPE_DECLINE, SHAPE_SUPPORT, SHAPE_DEFAULT)


@dataclass(frozen=True)
class Standard:
    """One situation's standard: the sentence the reviewer reads and the limits the measurements are held to."""
    shape: str
    text: str
    max_questions: Optional[int] = None
    max_chars: Optional[int] = None
    max_list_lines: Optional[int] = None
    max_sentences: Optional[int] = None
    max_exclamations: Optional[int] = None
    forbid_markup: bool = False          # no markdown, no quotation marks (a reply that is read aloud)
    max_figures: Optional[int] = None    # at most this many figures (dates excluded) in the reply
    voice_check: bool = False            # no first-person words copied from a record; every sentence with a figure or date traces to what was asked
    states_a_fact_date: bool = False     # when the planner's own ``facts_read`` hold a date, the reply states at least one of them


STANDARDS: Dict[str, Standard] = {s.shape: s for s in (
    Standard(SHAPE_YES_NO,
             "A yes or no question: the first sentence is the verdict (yes, no, or not enough to say), decided by the newest "
             "record in the evidence, then at most two sentences of why. No plan, no list.",
             max_questions=1, max_chars=600, max_list_lines=0),
    Standard(SHAPE_ONE_STEP,
             "Where to start or what to do next: name ONE step, the one the person's records point to, in one or two sentences, "
             "and ask at most one question. No list of steps, no second option, no schedule.",
             max_questions=1, max_chars=500, max_list_lines=0),
    Standard(SHAPE_ASK_FIRST,
             "The reply asks for facts only the person holds: one sentence reacting, then the one or two questions, nothing else. "
             "No plan, list or assumed values.",
             max_questions=2, max_chars=500, max_list_lines=0),
    Standard(SHAPE_CLARIFY,
             "The message is cut off or says nothing to act on, and no known candidates exist to choose between: ONE question asking the person to "
             "supply the missing piece, in at most two sentences. No example topics, no list, no guess at what they meant, no greeting by a name.",
             max_questions=1, max_chars=240, max_list_lines=0, max_sentences=2),
    Standard(SHAPE_BRIEF,
             "The person asked for a quick or one-breath reply: the answer first, two or three short sentences, no list, no "
             "preamble, no closing offer.",
             max_questions=1, max_chars=450, max_list_lines=0),
    Standard(SHAPE_READBACK,
             "The person asked to be told what is stored or was done: the first sentence is the read-back itself, using the "
             "records' own names, cadence and dates exactly; nothing the records do not hold.",
             max_chars=900),
    Standard(SHAPE_DECLINE,
             "The person asked for something that cannot be done here (move money, change an account): sentence one declines, once, "
             "and sentence two offers the one real alternative. Two sentences, no list, no warning they did not ask for, no repeating.",
             max_questions=1, max_list_lines=0, max_sentences=2),
    Standard(SHAPE_SUPPORT,
             "The person is frustrated or wants to quit: sentence one acknowledges the feeling plainly (no lecture, no pep talk, no exclamation "
             "marks), sentence two gives the real facts from their records that change the picture, with the date the records hold, and "
             "sentence three is the ONE concrete next step that fits those facts. Three sentences at most, no list, no options menu.",
             max_questions=1, max_chars=500, max_list_lines=0, max_sentences=3, max_exclamations=0, states_a_fact_date=True),
    Standard(SHAPE_SPOKEN,
             "The reply will be read aloud: at most three plain spoken sentences, the answer first, no list, no markdown, no quotation marks, "
             "no question, and no more than two figures. Say each date as a person would say it.",
             max_questions=0, max_list_lines=0, max_sentences=3, forbid_markup=True, max_figures=2, voice_check=True),
    Standard(SHAPE_INFORMATION,
             "A question answered from the person's records: the answer first, at most four sentences, only what was asked, every figure "
             "with the date it was read.",
             max_sentences=4, voice_check=True),
    Standard(SHAPE_DEFAULT,
             "Every date, clock time and quoted name in the reply must come from the person's records or this turn's reads."),
)}

# The "found_constraint" situation (generic, not Quest-specific): the turn's evidence holds a fact that conflicts with what was asked.
# It applies on top of whichever shape the planner chose, so it is not one of STANDARDS. Whether a conflict exists is a judgment
# about meaning, so a structured model verdict makes it (Orchestrator.found_constraint_verdict), never matching words in the reply.
FOUND_CONSTRAINT_STANDARD = (
    "When the person's own records the turn read hold a fact that conflicts with what was asked (a day, a time, a limit), the reply "
    "states that fact and what it means FIRST, plainly (for example 'Friday will not work: <fact>'), and only then offers "
    "alternatives. It never leaves the conflicting request open as an option or asks whether to try it anyway.")


def found_constraint_hint(fact: str, consequence: str) -> str:
    """The repair for a reply that did not state a conflict the records hold, built from the verdict's own fields."""
    said = " ".join(x.strip().rstrip(".") + "." for x in (consequence, fact) if (x or "").strip())
    return ("open the reply by stating plainly" + (f" ({said})" if said else "")
            + " that the request will not work as asked and why; only then offer alternatives, and do not offer the conflicting "
              "request as an option (no 'go ahead anyway', no 'keep it and check for an exception') or ask whether to try it anyway")


def constraint_violated(verdict: Any) -> bool:
    """True when a found_constraint verdict says the records conflict with the request and the reply either does not state it
    first or still offers the conflicting request as an option. Reads the verdict's structured booleans only."""
    return bool(isinstance(verdict, dict) and verdict.get("conflict")
                and (not verdict.get("states_it_first") or verdict.get("leaves_request_open")))


def turn_holds_records(sources: Any, gathered: Any = None) -> bool:
    """True when the turn could have seen the person's own records: the assembled context carried a source with adapter
    ``records`` and items, or the planner made reads this turn (a record may have come in through a read, as in the cards and
    quest notes it opens on a booking request). Structured attribution only; no other turn pays for the judge."""
    if any(isinstance(src, dict) and src.get("adapter") == "records" and (src.get("items") or []) for src in (sources or [])):
        return True
    return any(isinstance(entry, dict) and str(entry.get("text") or "").strip() for entry in (gathered or []))


def found_constraint_head(fact: str, consequence: str) -> str:
    """The plain opening line built from the verdict's own fields (no model wording added), for a reply the model did not write
    (an ask built from a verifier's question) so the conflict still comes first."""
    return " ".join(x.strip().rstrip(".") + "." for x in (consequence, fact) if (x or "").strip())


MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
MONTH_NUMBER = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
DAY_THEN_MONTH = re.compile(
    rf"\b(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*[-–]\s*(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)?\s+(?:of\s+)?"
    rf"(?P<mon>(?:{MONTHS})[a-z]*)\b\.?", re.IGNORECASE)
MONTH_THEN_DAY = re.compile(
    rf"\b(?P<mon>(?:{MONTHS})[a-z]*)\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*[-–]\s*(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)?\b",
    re.IGNORECASE)
ISO_DATE = re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?!\d)")
CLOCK_TIME = re.compile(r"(?<![\d:.])(?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?\s*(?P<ap>[ap]\.?m\.?)?(?![\d:])", re.IGNORECASE)
HOUR_AMPM = re.compile(r"(?<![\d:.])(?P<h>\d{1,2})\s*(?P<ap>[ap])\.?m\.?\b", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
FIGURE = re.compile(
    r"(?:(?P<sym>[$€£])\s?(?P<a>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"|(?<![\w.])(?P<b>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(?:(?P<unit>USD|EUR|GBP|dollars?|euros?|pounds?)\b|(?P<pct>%)))", re.IGNORECASE)
MAX_FIGURES_COMPARED = 250
QUOTED_NAME = re.compile(r"[\"“](?P<name>[^\"“”\n]{3,80})[\"”]")
LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+\S", re.MULTILINE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(*\[])|\n+")


def month_of(token: str) -> Optional[int]:
    return MONTH_NUMBER.get(token.lower()[:3])


def dates_in(text: str) -> List[Tuple[str, List[Tuple[int, int]]]]:
    """Every calendar date the text states, as ``(the text of it, [(day, month), ...])`` (a range lists each endpoint)."""
    found: List[Tuple[str, List[Tuple[int, int]]]] = []
    spans: List[Tuple[int, int]] = []
    for rx in (DAY_THEN_MONTH, MONTH_THEN_DAY):
        for m in rx.finditer(text or ""):
            month = month_of(m.group("mon"))
            if month is None or any(a < m.end() and m.start() < b for a, b in spans):
                continue
            days = [int(m.group("d1"))] + ([int(m.group("d2"))] if m.group("d2") else [])
            if any(not 1 <= d <= 31 for d in days):
                continue
            spans.append((m.start(), m.end()))
            found.append((m.group(0).strip(" ."), [(d, month) for d in days]))
    for m in ISO_DATE.finditer(text or ""):
        month, day = int(m.group("m")), int(m.group("d"))
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.append((m.group(0), [(day, month)]))
    return found


def clock_key(hour: str, minute: str, ampm: Optional[str]) -> str:
    h = int(hour)
    suffix = (ampm or "").lower().replace(".", "")[:1]
    return f"{h}:{minute}{suffix}"


def clocks_in(text: str) -> List[Tuple[str, str]]:
    """Every clock time or stopwatch reading the text states, as ``(the text of it, a comparable key)``."""
    found: List[Tuple[str, str]] = []
    for m in CLOCK_TIME.finditer(text or ""):
        found.append((m.group(0).strip(), clock_key(m.group("h"), m.group("m"), m.group("ap"))))
    for m in HOUR_AMPM.finditer(text or ""):
        found.append((m.group(0).strip(), clock_key(m.group("h"), "00", m.group("ap"))))
    return found


def number_value(token: str) -> float:
    return float(token.replace(",", ""))


def held_figures(evidence: str) -> frozenset:
    """The whole numbers a text states (a figure held outright), capped so a long text stays cheap to compare."""
    held = list(dict.fromkeys(number_value(m.group(0)) for m in NUMBER.finditer(evidence or "")))[:MAX_FIGURES_COMPARED * 8]
    return frozenset(round(v) for v in held)


def figure_claims(draft: str) -> List[Tuple[str, float, bool]]:
    """(text, value, is_percent) for each currency amount or percentage the draft states."""
    out: List[Tuple[str, float, bool]] = []
    for m in FIGURE.finditer(draft or ""):
        token = m.group("a") or m.group("b")
        out.append((m.group(0).strip(), number_value(token), bool(m.group("pct"))))
    return out


COMPUTED_MARK = "Computed by code"     # a consumer starts a read result's computed block with this line (totals, minimum, maximum, average over the rows read)


def computed_figures(text: str) -> frozenset:
    """The whole numbers in the blocks of ``text`` that begin with COMPUTED_MARK (to the next blank line): numbers a program computed
    from the rows the turn read, which a reply may build a difference or ratio on."""
    found = set()
    for block in re.split(r"\n\s*\n", text or ""):
        at = block.find(COMPUTED_MARK)
        if at >= 0:
            found.update(held_figures(block[at:]))
    return frozenset(found)


def derived_from(value: int, stated: List[int], computed: frozenset, percent: bool) -> bool:
    """Whether ``value`` is the sum, difference, small multiple (or, for a percentage, the ratio) of a figure the draft states and
    another figure the draft states or a program computed this turn. Both operands come from the draft or from code, never from
    the bulk of what was read, so a long read cannot vouch for a wrong figure by coincidence."""
    for x in stated:
        for y in list(stated) + list(computed):
            if percent:
                if y and (round(100 * x / y) == value or (x and round(100 * y / x) == value)):
                    return True
            elif x + y == value or abs(x - y) == value:
                return True
        if not percent and x and any(x * k == value for k in range(2, 13)):
            return True
    return False


def unsupported_figures(draft: str, evidence: "EvidenceIndex") -> List[str]:
    """Currency amounts and percentages the draft states that the turn neither held nor derives: not in anything it held, and not the sum,
    difference, small multiple or ratio of a figure the draft states and a number the turn's reads held. Pure arithmetic on numbers; no wording."""
    claims = figure_claims(draft)
    amounts = [round(v) for _, v, pct in claims if not pct]
    out: List[str] = []
    for text, value, percent in claims:
        whole = round(value)
        if whole in evidence.everything:
            continue
        if derived_from(whole, [x for x in amounts if x != whole], evidence.computed, percent):
            continue
        out.append(text)
    return out


def computed_comparisons(draft: str, evidence: "EvidenceIndex") -> str:
    """Code-computed differences between the currency amounts the draft states that the turn held, for the writer to use instead of
    working them out itself: "2,340 - 2,000 = 340 (2,340 is 340 more than 2,000)"."""
    stated = list(dict.fromkeys(v for _, v, pct in figure_claims(draft) if not pct and round(v) in evidence.everything))[:5]
    lines: List[str] = []
    for i, a in enumerate(stated):
        for b in stated[i + 1:]:
            hi, lo = max(a, b), min(a, b)
            lines.append(f"{hi:,.0f} - {lo:,.0f} = {hi - lo:,.0f} ({hi:,.0f} is {hi - lo:,.0f} more than {lo:,.0f}; {lo:,.0f} is {hi - lo:,.0f} less than {hi:,.0f})")
    return "; ".join(lines)


def computed_sentence(draft: str, evidence: "EvidenceIndex") -> str:
    """One sentence written by code from the two largest currency amounts the draft states that the turn held: "2,340 USD is 340 USD more than 2,000 USD."
    Empty when the draft states fewer than two. The units are copied from the draft's own figures; nothing in it is a model's arithmetic."""
    held = [(v, t) for t, v, pct in figure_claims(draft) if not pct and round(v) in evidence.everything]
    held = sorted(dict(held).items(), key=lambda item: -item[0])[:2]
    if len(held) < 2 or held[0][0] == held[1][0]:
        return ""
    (hi, hi_text), (lo, lo_text) = held
    gap = f"{hi - lo:,.0f}"
    unit = hi_text.replace(f"{hi:,.0f}", gap, 1) if f"{hi:,.0f}" in hi_text else gap
    return f"{hi_text} is {unit} more than {lo_text}."


CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class EvidenceIndex:
    """What the turn held, indexed for the comparisons: the dates and clock keys it states, and its text squashed for names."""
    days: frozenset
    clocks: frozenset
    squashed: str
    everything: frozenset = frozenset()     # every whole number anywhere the turn held
    computed: frozenset = frozenset()       # the whole numbers a program computed over the rows the turn read (see COMPUTED_MARK)

    @classmethod
    def of(cls, evidence: str, reads: Optional[str] = None, figure_basis: Optional[str] = None) -> "EvidenceIndex":
        days = frozenset(d for _, parts in dates_in(evidence) for d in parts)
        # A clock key with no am/pm marker in the draft matches the same time with either marker in the evidence.
        clocks = set()
        for _, key in clocks_in(evidence):
            clocks.add(key)
            clocks.add(key.rstrip("ap"))
        everything = held_figures(evidence if figure_basis is None else figure_basis)
        return cls(days=days, clocks=frozenset(clocks), squashed=" " + squash(evidence) + " ", everything=everything,
                   computed=computed_figures(reads if reads is not None else evidence))


@dataclass
class Violation:
    kind: str          # "unsupported" | "questions" | "list" | "length"
    detail: str        # the exact repair, stated for the writer
    values: Tuple[str, ...] = ()


@dataclass
class Measurements:
    """The deterministic facts about one draft. ``violations`` is empty when every measurement meets the standard."""
    shape: str
    questions: int = 0
    list_lines: int = 0
    chars: int = 0
    unsupported: List[str] = field(default_factory=list)
    violations: List[Violation] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.violations)

    def as_text(self) -> str:
        bits = [f"questions={self.questions}", f"list_lines={self.list_lines}", f"chars={self.chars}"]
        if self.unsupported:
            bits.append("not_in_any_evidence=" + "; ".join(f"'{v}'" for v in self.unsupported[:6]))
        text = ", ".join(bits)
        return text + (" | BREAKS: " + "; ".join(v.detail for v in self.violations) if self.violations else " | meets the limits")

    def hint(self) -> str:
        """The repair, one line the writer can follow without judgment."""
        return " ".join(v.detail.rstrip(".") + "." for v in self.violations)[:600]


def unsupported_values(draft: str, evidence: EvidenceIndex) -> List[str]:
    """Dates, clock times and quoted names the draft states that nothing the turn held states."""
    out: List[str] = []
    for text, parts in dates_in(draft):
        if any(p not in evidence.days for p in parts):
            out.append(text)
    for text, key in clocks_in(draft):
        if key not in evidence.clocks and key.rstrip("ap") not in evidence.clocks:
            out.append(text)
    for m in QUOTED_NAME.finditer(draft or ""):
        name = squash(m.group("name"))
        if len(name.split()) >= 2 and f" {name} " not in evidence.squashed:
            out.append(m.group("name"))
    out.extend(unsupported_figures(draft, evidence))
    return list(dict.fromkeys(out))


FROM_TODAY = re.compile(r"(?<!\d)(?P<n>\d{1,3})\s+days?\s+(?:away\s+)?from\s+(?:today|now)\b", re.IGNORECASE)
TODAY_IN_EVIDENCE = re.compile(r"(?:today|current date|now)\b[^\n\d]{0,40}(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})", re.IGNORECASE)
LIVE_DATE_LINE = re.compile(r"Date:\s*(?:[A-Za-z]+,\s*)?(?P<mon>[A-Za-z]+)\s+(?P<d>\d{1,2}),\s*(?P<y>\d{4})")


def today_of(evidence: str):
    """(today, tolerance in days): the date the turn's own context states, else the server's date with one day of slack
    (the person's zone may be a day away from the server's)."""
    from datetime import date
    match = TODAY_IN_EVIDENCE.search(evidence or "")
    try:
        if match:
            return date(int(match.group("y")), int(match.group("m")), int(match.group("d"))), 0
        live = LIVE_DATE_LINE.search(evidence or "")
        if live and month_of(live.group("mon")):
            return date(int(live.group("y")), month_of(live.group("mon")), int(live.group("d"))), 0
    except ValueError:
        pass
    return date.today(), 1


def day_count_errors(draft: str, evidence: str) -> List[str]:
    """Each "N days from today" the draft states beside a calendar date when N is not the real distance from the turn's own
    today to that date. Pure arithmetic on dates the text states (never on its wording): the model keeps miscounting these.
    Only the explicit "from today / from now" form is judged, because "3 days before they arrive" counts from another date."""
    from datetime import date
    today, slack = today_of(evidence)
    errors: List[str] = []
    for sentence in SENTENCE_END.split(draft or ""):
        claim = FROM_TODAY.search(sentence)
        found = dates_in(sentence)
        if not claim or len(found) != 1 or len(found[0][1]) != 1:
            continue
        day, month = found[0][1][0]
        try:
            target = date(today.year, month, day)
        except ValueError:
            continue
        if target < today:
            continue
        real = (target - today).days
        if abs(int(claim.group("n")) - real) > slack:
            errors.append(f"{real} days from today to {found[0][0]}, not {claim.group('n')}")
    return errors


RELATIVE_TO_EVENT = re.compile(
    r"(?<!\d)(?P<n>\d{1,3})\s+days?\s+(?P<dir>before|after)\s+(?P<event>[A-Za-z][A-Za-z' ]{2,30}?)\s*\((?P<when>[^()]{3,25})\)",
    re.IGNORECASE)


def event_offset_errors(draft: str) -> List[str]:
    """Each "N days before/after <event> (D)" whose D is not the date N days from the date the SAME paragraph states just
    before it (IMP-027: "arrive 13 Nov ... 3 days before arrival (17 Nov)" when 3 days before 13 Nov is 10 Nov). Pure date
    arithmetic on dates the text states; it judges only this explicit parenthesised form, and only when a date comes earlier in
    the paragraph to count from."""
    from datetime import date, timedelta
    errors: List[str] = []
    for paragraph in (draft or "").split("\n\n"):
        for claim in RELATIVE_TO_EVENT.finditer(paragraph):
            earlier = dates_in(paragraph[:claim.start()])
            stated = dates_in(claim.group("when"))
            if not earlier or len(stated) != 1 or len(stated[0][1]) != 1 or len(earlier[-1][1]) != 1:
                continue
            (sd, sm), (ed, em) = stated[0][1][0], earlier[-1][1][0]
            try:
                year = date.today().year
                anchor, claimed = date(year, em, ed), date(year, sm, sd)
            except ValueError:
                continue
            n = int(claim.group("n"))
            expected = anchor - timedelta(days=n) if claim.group("dir").lower() == "before" else anchor + timedelta(days=n)
            if claimed != expected:
                errors.append(f"{n} days {claim.group('dir').lower()} {earlier[-1][0]} is {expected.day} "
                              f"{expected:%b}, not {stated[0][0]}")
    return errors


CLARIFY_DIRECTIVE = (
    "The person's message is cut off or ambiguous, and you are given nothing else about them or their work. Reply with ONE short question "
    "that asks them to finish or say what they want, in at most two sentences. Refer only to what the message itself says. Give no example "
    "topics, no list, no guess, and do not greet them by a name.")


VOICE_NOTE = (
    " Records may be written in the first person by whoever authored them: never copy I, my, me or our from a record, say whose it is by name "
    "(\"waiting for Zee's preview\"). Use \"I\" only for what you yourself did or could not do this turn.")

SPOKEN_DIRECTIVE = (
    "--- THIS REPLY IS READ ALOUD (set by the planner) ---\n"
    "The person will read your reply out loud. Write at most three plain sentences a person can say without stumbling: the answer first, "
    "then the one fact that matters most with the date it was read (\"as of 5 November\", said as a person would). No list, no markdown, "
    "no quotation marks, no question, no more than two figures, nothing that must be seen on a page. Never use an em dash."
    + VOICE_NOTE)


GENERAL_ANSWER_DIRECTIVE = (
    "The person asked a general question (a definition, how to use the product, or a greeting). You are given nothing about them or their work, "
    "on purpose. Answer it from what you know about the subject in one or two plain sentences, the answer first, with no example drawn from "
    "their goals, notes or numbers, no offer to look anything up and no remark about what you were not given. Never use an em dash.")


def is_clarify(plan: Any) -> bool:
    """True when the planner's structured reply shape says the reply is the one question a cut-off or ambiguous message needs (no known candidates to choose
    between: a CHOICE among known candidates is ``ask_first``, which keeps its context)."""
    return getattr(plan, "action", None) == "answer" and getattr(plan, "reply_shape", None) == SHAPE_CLARIFY


def standard_for(plan: Any, *, user_intent: Optional[str] = None, delivery: Optional[str] = None) -> Standard:
    """The standard that applies to this reply, from the planner's structured fields only. ``user_intent`` and ``delivery`` are the turn's
    latest reports from any planner step (the last step may be a read that did not repeat them); they default to the plan's own."""
    user_intent = user_intent or getattr(plan, "user_intent", None)
    delivery = delivery or getattr(plan, "delivery", None)
    shape = getattr(plan, "reply_shape", None)
    if getattr(plan, "asks_first", False) and shape != SHAPE_CLARIFY:
        shape = SHAPE_ASK_FIRST
    if delivery == "spoken" and shape not in (SHAPE_ASK_FIRST, SHAPE_CLARIFY, SHAPE_DECLINE, SHAPE_SUPPORT):
        shape = SHAPE_SPOKEN
    if shape in (None, SHAPE_DEFAULT) and user_intent == "ask":
        shape = SHAPE_INFORMATION
    return STANDARDS.get(shape or "", STANDARDS[SHAPE_DEFAULT])


def measure(draft: str, standard: Standard, evidence: str, reads: Optional[str] = None, figure_basis: Optional[str] = None,
            facts: Optional[List[str]] = None, resolved: Any = None, question: Optional[str] = None) -> Measurements:
    """Measure one draft against its situation's standard. Pure and never raises (a failure measures nothing). ``facts`` are the planner's own
    ``facts_read`` quotes (structured), used only by a standard that asks the reply to state a date those facts hold."""
    try:
        text = draft or ""
        m = Measurements(shape=standard.shape, questions=text.count("?"), list_lines=len(LIST_LINE.findall(text)), chars=len(text))
        index = EvidenceIndex.of(evidence, reads, figure_basis)
        m.unsupported = unsupported_values(text, index)
        if m.unsupported:
            m.violations.append(Violation(
                "unsupported", "remove " + ", ".join(f"'{v}'" for v in m.unsupported[:4])
                + " (it is in nothing you were given; say you do not have it, or leave it out)", tuple(m.unsupported)))
            computed = computed_comparisons(text, index)
            if computed and any(FIGURE.fullmatch(v) for v in m.unsupported):
                m.violations[-1] = Violation(
                    "unsupported", m.violations[-1].detail + ". A figure is wrong because it is not the sum or difference of the figures you were given. "
                    "Use these computed by code, not your own arithmetic: " + computed, tuple(m.unsupported))
        if resolved:      # facts that had two dated readings (``fact_supersession``): a draft stating an older one as current breaks
            m.violations.extend(resolved.violations(text))
        elif resolved is not None:
            m.violations.extend(resolved.contradictions(text))
        for wrong in day_count_errors(text, evidence):
            m.violations.append(Violation("days", "correct the day count: it is " + wrong, (wrong,)))
        for wrong in event_offset_errors(text):
            m.violations.append(Violation("days", "correct the date: " + wrong, (wrong,)))
        if standard.max_questions is not None and m.questions > standard.max_questions:
            m.violations.append(Violation(
                "questions", f"ask at most {standard.max_questions} question{'s' if standard.max_questions != 1 else ''}"
                f" (you asked {m.questions}); keep the one that lets you act"))
        if standard.max_list_lines is not None and m.list_lines > standard.max_list_lines:
            m.violations.append(Violation("list", f"write it as plain sentences, not a list ({m.list_lines} list lines)"))
        if standard.max_sentences is not None and len(sentences(text)) > standard.max_sentences:
            m.violations.append(Violation("length", f"write it in at most {standard.max_sentences} sentences (it has {len(sentences(text))}); "
                                          "keep the part that answers, drop the rest"))
        if standard.max_chars is not None and m.chars > standard.max_chars:
            m.violations.append(Violation("length", f"cut it to under {standard.max_chars} characters (it is {m.chars})"))
        if standard.voice_check:
            copied = copied_first_person(text, [evidence, figure_basis or ""])
            if copied:
                m.violations.append(Violation(
                    "voice", "you copied first-person words from a record (" + ", ".join(f"'{c}'" for c in copied[:3]) + "): the record's author is "
                    "not you. Say whose it is by name (for example the person or team the record names), or write \"we\" only when the speaker is "
                    "the team; use \"I\" only for what you yourself did or could not do this turn", tuple(copied)))
            if question:
                for stray in unrelated_sentences(text, question, facts):
                    m.violations.append(Violation("unrelated", f"remove or tie to the question this sentence, which names a figure or date the "
                                                  f"question did not ask about: {stray[:100]!r}", (stray,)))
                    break
        if standard.forbid_markup and re.search(r"[*#`_>]|[\"“”]", text):
            m.violations.append(Violation("format", "write plain words only, with no asterisks, hashes, backticks, underscores or quotation marks"))
        if standard.max_figures is not None:
            said_figures = len(NUMBER.findall(_without_dates(text)))
            if said_figures > standard.max_figures:
                m.violations.append(Violation("figures", f"state at most {standard.max_figures} figures (you stated {said_figures}); keep the one that matters most"))
        if standard.max_exclamations is not None and text.count("!") > standard.max_exclamations:
            m.violations.append(Violation("tone", "remove the exclamation marks; keep the tone plain and steady"))
        if standard.states_a_fact_date:
            held_dates = [text_of for text_of, _ in dates_in(" ".join(facts or []))]
            if held_dates and not dates_in(text):
                m.violations.append(Violation("date", f"state the date your records hold ({held_dates[0]}) in the second sentence"))
        return m
    except Exception:  # noqa: BLE001 -- measuring must never break a turn
        return Measurements(shape=standard.shape)


def _without_dates(text: str) -> str:
    for stated, _ in dates_in(text or ""):
        text = text.replace(stated, " ")
    return text


FIRST_PERSON = frozenset(("i", "my", "me", "mine", "myself", "our", "ours"))
OVERLAP_STOP = frozenset("that this with from have were been what when where which their there about would could should your they them then than also into over only just more most some very much many".split())


def copied_first_person(draft: str, evidence_texts: List[str]) -> List[str]:
    """First-person words the draft took from a record: a first-person word with the two words after it, said the same way in the evidence.
    The assistant speaking about its own action this turn is not copied from a record, so it never matches; text authored in the first
    person by someone else ("awaiting my preview before it goes out") does. Compares the words of the draft with the evidence; reads no meaning."""
    held = " " + " ".join(squash(t) for t in evidence_texts if t) + " "
    words = re.findall(r"[A-Za-z']+", draft or "")
    out: List[str] = []
    for i, word in enumerate(words):
        if word.lower() in FIRST_PERSON and i + 2 < len(words):
            stretch = " ".join(words[i:i + 3])
            if f" {squash(stretch)} " in held:
                out.append(stretch)
    return list(dict.fromkeys(out))


def topic_words(text: str) -> frozenset:
    return frozenset(w for w in squash(text).split() if len(w) >= 4 and w not in OVERLAP_STOP)


def unrelated_sentences(draft: str, question: str, facts: Optional[List[str]]) -> List[str]:
    """Sentences that state a figure or a date and share no topic word with what the question asked or with the facts the planner reported
    reading for it (``facts_read``, its own structured account of what bears on the request). Needs the facts: with none reported there is
    no basis and nothing is flagged."""
    basis = topic_words(question) | topic_words(" ".join(facts or []))
    if not facts or not basis:
        return []
    out: List[str] = []
    for sentence in sentences(draft):
        if (dates_in(sentence) or NUMBER.search(_without_dates(sentence))) and not (topic_words(sentence) & basis):
            out.append(sentence.strip())
    return out


def sentences(text: str) -> List[str]:
    return [s for s in SENTENCE_END.split(text or "") if s and s.strip()]


def repair(draft: str, standard: Standard, measurements: Measurements, evidence: str, reads: Optional[str] = None,
           figure_basis: Optional[str] = None) -> str:
    """The cheapest deterministic repair that is safe, for a draft that still breaks a measurement after the repair rounds.

    Drops each sentence that states an unsupported value and every question after the allowed number. It never empties the reply:
    when the repair would leave nothing, the draft is returned as it was. A list or a length breach has no safe mechanical repair
    (cutting mid-thought changes the meaning), so it ships as written.
    """
    try:
        if not measurements.violations:
            return draft
        stray = {x.strip() for v in measurements.violations if v.kind == "unrelated" for x in v.values}
        parts = [x for x in sentences(draft) if x.strip() not in stray] or sentences(draft)
        evidence_index = EvidenceIndex.of(evidence, reads, figure_basis)
        kept: List[str] = []
        asked = 0
        trimmed_figure = False
        for sentence in parts:
            flagged = unsupported_values(sentence, evidence_index) if measurements.unsupported else []
            if flagged:
                trimmed_figure = trimmed_figure or any(FIGURE.fullmatch(v) for v in flagged)
                # keep the clauses that state nothing unsupported (split at commas, semicolons and colons: punctuation, not wording)
                clauses = CLAUSE_SPLIT.split(sentence)
                good = [c for c in clauses if not unsupported_values(c, evidence_index)]
                if good and len(good) < len(clauses):
                    sentence = " ".join(good).strip().rstrip(",;:") + "."
                else:
                    continue
            if "?" in sentence and standard.max_questions is not None:
                asked += sentence.count("?")
                if asked > standard.max_questions:
                    continue
            kept.append(sentence.strip())
        if kept and trimmed_figure:
            relation = computed_sentence(draft, evidence_index)
            if relation:
                kept.append(relation)
        if standard.max_sentences is not None and len(kept) > standard.max_sentences >= 2:
            # Last resort for a length breach the rewrites did not fix: the opening sentences and the closing one (the decline and its
            # alternative, the acknowledgement and the next step). Position only; no wording is read.
            kept = kept[:standard.max_sentences - 1] + kept[-1:]
        fixed = " ".join(kept) if kept else draft
        if standard.forbid_markup:      # the last resort for a reply read aloud: the marks go, the words stay
            fixed = re.sub(r"[*#`_>]|[\"“”]", "", fixed)
        return fixed
    except Exception:  # noqa: BLE001
        return draft


def steering(hint: str, previous: str) -> str:
    """The text a regeneration is steered with: the reviewer's hint and the answer it concerns."""
    return f"A reviewer flagged this for one correction: {hint}\n\n--- YOUR PREVIOUS ANSWER ---\n{previous}"


def keep_addresses_hint(draft: str, held: Any) -> str:
    """The sentence added to a repair hint when the draft holds web addresses: they must survive the repair (the length cap yields)."""
    if not held:
        return ""
    return " Keep every web address in the answer exactly as written, even if that leaves it longer than the limit."
