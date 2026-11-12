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

from hubofhubs.api import Unit, cohere, dangling, dates_in, day_count_errors, event_offset_errors, joined, shorten, units_of

SHAPE_YES_NO = "yes_no"
SHAPE_ONE_STEP = "one_step"
SHAPE_ASK_FIRST = "ask_first"
SHAPE_CLARIFY = "clarify"
SHAPE_BRIEF = "brief"
SHAPE_READBACK = "readback"
SHAPE_DECLINE = "decline"
SHAPE_SUPPORT = "support"
SHAPE_ADVICE = "advice"            # a general how-to or "what can I do to improve X" question the person's records need not settle
SHAPE_DEFAULT = "default"
SHAPE_SPOKEN = "spoken"            # selected from the planner's ``delivery`` field, not offered as a ``reply_shape``
SHAPE_INFORMATION = "information"  # selected from the planner's ``user_intent`` (ask) on a default shape
SHAPE_PLAN = "plan"                # selected only by code (a ``plans_period`` turn answered from the planning snapshot), never offered to the planner
SHAPE_SETTLED = "settled"          # selected from the planner's ``user_intent`` (act) when it answers on a default shape without a deliverable written in the reply
SHAPE_STRUCTURE_TAIL = "structure_tail"      # the closing paragraph of a framed or compared reply (``structure_reply``), written by its own call and measured here; not offered as a ``reply_shape``
REPLY_SHAPES = (SHAPE_YES_NO, SHAPE_ONE_STEP, SHAPE_ASK_FIRST, SHAPE_CLARIFY, SHAPE_BRIEF, SHAPE_READBACK, SHAPE_DECLINE, SHAPE_SUPPORT, SHAPE_ADVICE, SHAPE_DEFAULT)


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
    Standard(SHAPE_ADVICE,
             "A general question about how to practice, learn, improve or do something, which the person's records need not settle: a FULL "
             "answer that opens with the answer itself, three to five distinct, concrete things to do (a short list is welcome), each with one "
             "clause on how, fitted to what their quest is about when that helps. Say once, in a few plain words, that it is general knowledge "
             "(or what it rests on) and how sure you are. No apology, no remark about what their records lack, and never one vague sentence."),
    Standard(SHAPE_SPOKEN,
             "The reply will be read aloud: at most three plain spoken sentences, the answer first, no list, no markdown, no quotation marks, "
             "no question, and no more than two figures. Say each date as a person would say it.",
             max_questions=0, max_list_lines=0, max_sentences=3, forbid_markup=True, max_figures=2, voice_check=True),
    Standard(SHAPE_INFORMATION,
             "A question answered from the person's records: the answer first (for a status, the current value of its key measure with the date it was "
             "read; for a count, the count), at most three plain sentences, only what was asked, every figure with the date it was read. No list, no "
             "offer, no question back, no extra readings or suggestions.",
             max_sentences=3, max_list_lines=0, voice_check=True),
    Standard(SHAPE_SETTLED,
             "An order that was carried out or found already in place: the first sentence says what is now so (done, or already there, with where it is "
             "and when it next happens), at most two sentences in all. No offer, no question, no list, nothing that was not asked.",
             max_questions=1, max_list_lines=0, max_sentences=2, voice_check=True),
    Standard(SHAPE_PLAN,
             "A plan for a coming period laid out from the person's own open goals: overdue goals first, then one short line per day naming the goal "
             "it serves (or a few lines, when the goals are few), every goal named exactly as the records hold it and on or before its due day. When "
             "the quest has no open goals, propose at most three first goals that move its outcome as day lines, and say in the first sentence that they "
             "are proposals for a quest with no open goals (a code-written opening that says it counts). "
             "Say nothing was saved, and end with at most one question (whether to add it to their goals).",
             # No list-line cap: a cut list line leaves a plan with days missing and broken fragments (measured 2026-11-11 with a cap of 14 on a
             # quest with eight overdue goals). The planning snapshot already bounds what the plan can hold.
             max_questions=1, voice_check=True),
    Standard(SHAPE_STRUCTURE_TAIL,
             "The closing paragraph that ties a reply to the structure of the person's intent: at most three short sentences and about sixty words in all, one idea per sentence "
             "(what it serves and what good looks like; the alternatives and the metric that shows which works, with its value; the next milestone), no list, no question.",
             max_questions=0, max_chars=420, max_list_lines=0, max_sentences=3),
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


CLOCK_TIME = re.compile(r"(?<![\d:.])(?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?\s*(?P<ap>[ap]\.?m\.?)?(?![\d:])", re.IGNORECASE)
HOUR_AMPM = re.compile(r"(?<![\d:.])(?P<h>\d{1,2})\s*(?P<ap>[ap])\.?m\.?\b", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
FIGURE = re.compile(
    r"(?:(?P<sym>[$€£])\s?(?P<a>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"|(?<![\w.])(?P<b>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s?(?:(?P<unit>USD|EUR|GBP|dollars?|euros?|pounds?)\b|(?P<pct>%)))", re.IGNORECASE)
MAX_FIGURES_COMPARED = 250
MAX_WINDOW_LINES = 20000
QUOTED_NAME = re.compile(r"[\"“](?P<name>[^\"“”\n]{3,80})[\"”]")
LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+\S", re.MULTILINE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(*\[])|\n+")




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
    windows: tuple = ()                     # per evidence line with its two lines before it: (the dates it states, the whole numbers it states)
    listed: str = " "                       # the evidence's list lines (goals, steps), squashed: a number inside one is part of a name, not a reading

    @classmethod
    def of(cls, evidence: str, reads: Optional[str] = None, figure_basis: Optional[str] = None) -> "EvidenceIndex":
        days = frozenset(d for _, parts in dates_in(evidence) for d in parts)
        # A clock key with no am/pm marker in the draft matches the same time with either marker in the evidence.
        clocks = set()
        for _, key in clocks_in(evidence):
            clocks.add(key)
            clocks.add(key.rstrip("ap"))
        everything = held_figures(evidence if figure_basis is None else figure_basis)
        lines = (evidence or "").splitlines()[:MAX_WINDOW_LINES]
        windows = []
        for i in range(len(lines)):
            back = i
            while back > max(0, i - 2) and lines[back - 1].strip():      # the two lines before it, never across a blank line
                back -= 1
            near = "\n".join(lines[back:i + 1])
            when = frozenset(d for _, parts in dates_in(near) for d in parts)
            if when:
                windows.append((when, held_figures(without_dates(near))))
        listed = " " + " | ".join(squash(line) for line in lines if LIST_LINE.match(line)) + " "
        return cls(days=days, clocks=frozenset(clocks), squashed=" " + squash(evidence) + " ", everything=everything,
                   computed=computed_figures(reads if reads is not None else evidence), windows=tuple(windows), listed=listed)


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
    out.extend(misdated_figures(draft, evidence))
    return list(dict.fromkeys(out))


NUMBER_WITH_WORDS = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[\s,;:()-]+[A-Za-z][\w'-]*){2}")


def named_figures_out(sentence: str, evidence: "EvidenceIndex") -> str:
    """``sentence`` without the numbers that, with the two words after them, stand as they are in one of the evidence's list lines: the number is part of a
    listed goal's own name ("Run 10 customer interviews") or of what the list says beside it ("10 days overdue"), not a reading the sentence dates.
    Measured 2026-11-11: a week plan that put "Run 10 customer interviews" on Thursday 12 November had that date cut as carried from another fact."""
    def keep(m: "re.Match[str]") -> str:
        return " " if f" {squash(m.group(0))} " in evidence.listed or squash(m.group(0)) in evidence.listed else m.group(0)
    return NUMBER_WITH_WORDS.sub(keep, sentence or "") if evidence.listed.strip() else sentence


def misdated_figures(draft: str, evidence: "EvidenceIndex") -> List[str]:
    """Dates a sentence of the draft states beside a figure when no place in the turn's evidence (a line and the two before it) states that date near any of that
    sentence's figures: the date is carried from another fact, not from the reading the figure came from. Pure comparison of dates and numbers; no wording."""
    if not evidence.windows:
        return []
    out: List[str] = []
    for sentence in sentences(draft):
        found = dates_in(sentence)
        numbers = held_figures(named_figures_out(without_dates(sentence), evidence))
        if not found or not numbers:
            continue
        for text, parts in found:
            if not any(all(p in when for p in parts) and (numbers & figures) for when, figures in evidence.windows):
                out.append(text)
    return out




CLARIFY_DIRECTIVE = (
    "The person's message is cut off or ambiguous, and you are given nothing else about them or their work. Reply with ONE short question "
    "that asks them to finish or say what they want, in at most two sentences. Refer only to what the message itself says. Give no example "
    "topics, no list, no guess, and do not greet them by a name.")


VOICE_NOTE = (
    " Records may be written in the first person by whoever authored them: never copy I, my, me or our from a record, say whose it is by name "
    "(\"waiting for Sam's preview\"). Use \"I\" only for what you yourself did or could not do this turn.")

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
    elif (shape in (None, SHAPE_DEFAULT) and user_intent == "act" and getattr(plan, "action", None) == "answer"
          and getattr(plan, "deep_target", None) != "draft_in_chat" and not getattr(plan, "deferred_deep", None)):
        shape = SHAPE_SETTLED
    return STANDARDS.get(shape or "", STANDARDS[SHAPE_DEFAULT])


def measure(draft: str, standard: Standard, evidence: str, reads: Optional[str] = None, figure_basis: Optional[str] = None,
            facts: Optional[List[str]] = None, resolved: Any = None, question: Optional[str] = None, drafted: Any = ()) -> Measurements:
    """Measure one draft against its situation's standard. Pure and never raises (a failure measures nothing). ``facts`` are the planner's own
    ``facts_read`` quotes (structured), used only by a standard that asks the reply to state a date those facts hold. ``drafted`` is the text the reply
    hands over (``deliverable_texts``): its questions, list lines and sentences are the deliverable's, so the counts leave them out."""
    try:
        text = draft or ""
        own = prose_of(text, drafted)
        m = Measurements(shape=standard.shape, questions=own.count("?"), list_lines=len(LIST_LINE.findall(own)), chars=len(text))
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
        if standard.max_sentences is not None and prose_count(text, drafted) > standard.max_sentences:
            m.violations.append(Violation("length", f"write it in at most {standard.max_sentences} sentences (it has {prose_count(text, drafted)}), not counting a drafted text you hand over; "
                                          "keep the part that answers and any drafted text whole, drop the rest"))
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
            said_figures = figures_said(text)
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


def without_dates(text: str) -> str:
    """``text`` with its dates taken out, each with the year written after it ("7 November 2026", "7 November, 2026"): a year left behind would read as a figure."""
    for stated, _ in dates_in(text or ""):
        text = re.sub(re.escape(stated) + r"(?:,?\s+(?:19|20)\d{2}\b)?", " ", text)
    return text


FIRST_PERSON = frozenset(("i", "my", "me", "mine", "myself", "our", "ours"))
OVERLAP_STOP = frozenset("that this with from have were been what when where which their there about would could should your they them then than also into over only just more most some very much many".split())


def copied_first_person(draft: str, evidence_texts: List[str]) -> List[str]:
    """First-person words the draft took from a record: a first-person word with the two words after it, said the same way in the evidence.
    The assistant speaking about its own action this turn is not copied from a record, so it never matches; text authored in the first
    person by someone else ("awaiting my preview before it goes out") does. Compares the words of the draft with the evidence; reads no meaning."""
    held = " " + " ".join(squash(t) for t in evidence_texts if t) + " "
    # Words inside quotation marks are a name or a quote the reader sees as one ("i want to become self reliant" is a quest's own name): quoted, they are
    # attributed, not spoken in the assistant's voice (measured 2026-11-11: naming that quest was flagged, and its repair garbled the reply's opening).
    words = re.findall(r"[A-Za-z']+", QUOTED_NAME.sub(" ", draft or ""))
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
        if (dates_in(sentence) or NUMBER.search(without_dates(sentence))) and not (topic_words(sentence) & basis):
            out.append(sentence.strip())
    return out


RATIO = re.compile(r"\d[\d,.]*\s*%?\s+of\s+\d[\d,.]*")
PARENTHESIS = re.compile(r"\([^()]*\)")


def figures_said(text: str) -> int:
    """How many figures a reply states, as a listener counts them: dates are not figures, "5 of 10" is one, and the numbers in parentheses after a figure are its
    breakdown, not more figures. Counts numbers; reads no wording."""
    plain = PARENTHESIS.sub(" ", without_dates(text or ""))
    return len(NUMBER.findall(RATIO.sub(" 1 ", plain)))


def sentences(text: str) -> List[str]:
    return [s for s in SENTENCE_END.split(text or "") if s and s.strip()]


def drafted_blocks(text: str) -> List[str]:
    """The drafted text a reply hands over, found by layout, never by wording: the run of paragraphs from the first to the last paragraph holding a line that does not end
    like a sentence ("Subject: ...", "Hi,", a sign-off) or a list line, when there are two such paragraphs or one of several lines. Used only on a turn whose plan says the
    reply carries a deliverable (``deliverable_texts``): on any other turn a list is the reply's own and the limits count it."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    shaped = [i for i, p in enumerate(paragraphs)
              if any(line.strip() and (LIST_LINE.match(line) or not re.search(r"[.!?)\"”']\s*$", line.strip())) for line in p.splitlines())]
    if not shaped or (len(shaped) == 1 and len(paragraphs[shaped[0]].strip().splitlines()) < 2):
        return []
    return ["\n\n".join(paragraphs[shaped[0]:shaped[-1] + 1])]


def deliverable_texts(rs: Any, text: str) -> List[str]:
    """What the reply hands over, from structured state only: the draft a milestone step kept, and, when the plan says the deliverable is written in the reply
    (``draft_in_chat``) or a draft was kept, the drafted blocks of the reply's layout. Empty on every other turn."""
    kept = str(getattr(getattr(rs, "cue_resolved", None), "draft", "") or "").strip()
    out = [kept] if kept else []
    if kept or getattr(rs, "draft_in_chat", False) or getattr(getattr(rs, "plan", None), "deep_target", None) == "draft_in_chat":
        out += drafted_blocks(text)
    return out


LIST_PREFIX = re.compile(r"^\s*(?:>|[-*\u2022]|\d+[.)])\s*")


def in_drafted(sentence: str, drafted: Any) -> bool:
    plain = LIST_PREFIX.sub("", sentence or "").strip()
    return bool(plain) and any(plain in d or sentence in d for d in drafted or () if d)


def protected_units(text: str, drafted: Any = ()) -> List[Unit]:
    """The sentences of ``text`` with the ones a repair must keep marked: those inside the drafted text and those carrying a web address or a quoted name."""
    units = units_of(text)
    held = pointed_items(text)
    for u in units:
        u.protected = in_drafted(u.text, drafted) or any(item in u.text for item in held)
    return units


def prose_of(text: str, drafted: Any = ()) -> str:
    """The reply's own words: ``text`` without the sentences of a drafted text it hands over."""
    if not drafted:
        return text or ""
    return " ".join(u.text for u in units_of(text) if not in_drafted(u.text, drafted))


def prose_count(text: str, drafted: Any = ()) -> int:
    """How many sentences of the reply's own prose a length limit counts: a drafted text it hands over is not counted."""
    return sum(1 for u in units_of(text) if not in_drafted(u.text, drafted))


def orphaned_sentences(before: str, after: str, question: str = "") -> List[str]:
    """Sentences of ``after`` that point back to a sentence ``before`` held and ``after`` no longer does (the hub's ``dangling``: a pronoun opening right after a removed
    sentence, "the same", or a demonstrative whose noun only a removed sentence said). Reads grammar words only, never what a sentence says."""
    units = units_of(before)
    remaining = [u.text for u in units_of(after)]
    pool = list(remaining)
    for u in units:
        if u.text in pool:
            pool.remove(u.text)
        else:
            u.kept = False
    if pool:
        return []
    return [units[i].text for i in dangling(units, question)]


def drop_orphans(before: str, after: str, question: str = "") -> str:
    """``after`` without the sentences that point back to what the repair that turned ``before`` into it removed. Never empties the reply."""
    units = units_of(before)
    pool = [u.text for u in units_of(after)]
    for u in units:
        if u.text in pool:
            pool.remove(u.text)
        else:
            u.kept = False
    if pool or not any(u.kept for u in units):
        return after
    units, _ = cohere(units, question)
    return joined(units) or after


def repair(draft: str, standard: Standard, measurements: Measurements, evidence: str, reads: Optional[str] = None,
           figure_basis: Optional[str] = None, question: str = "", facts: Optional[List[str]] = None, drafted: Any = ()) -> str:
    """The cheapest deterministic repair that is safe, for a draft that still breaks a measurement after the repair rounds.

    Drops each sentence that states an unsupported value and every question after the allowed number; a length breach removes the sentences the reply needs least (the hub's
    ``shorten``: the answer, a sentence the question points to, a drafted text and a sentence with an address or quoted name are kept; a repeat or a sentence sharing no word
    with the question goes first), never by position. A sentence left pointing back to a removed one goes too. It never empties the reply: when the repair would leave
    nothing, the draft is returned as it was. A list breach has no safe mechanical repair, so it ships as written.
    """
    try:
        if not measurements.violations:
            return draft
        stray = {x.strip() for v in measurements.violations if v.kind == "unrelated" for x in v.values}
        units = protected_units(draft, drafted)
        if all(u.text.strip() in stray for u in units):
            stray = set()
        evidence_index = EvidenceIndex.of(evidence, reads, figure_basis)
        asked = 0
        trimmed_figure = False
        for u in units:
            if u.text.strip() in stray and not u.protected:
                u.kept = False
                continue
            flagged = unsupported_values(u.text, evidence_index) if measurements.unsupported else []
            if flagged:
                trimmed_figure = trimmed_figure or any(FIGURE.fullmatch(v) for v in flagged)
                # keep the clauses that state nothing unsupported (split at commas, semicolons and colons: punctuation, not wording)
                clauses = CLAUSE_SPLIT.split(u.text)
                good = [c for c in clauses if not unsupported_values(c, evidence_index)]
                if good and len(good) < len(clauses):
                    # Rejoined as a sentence: no full stop doubled ("...actually play..") and no lowercase "and" left at its start (measured live
                    # 2026-11-11 after a clause holding a proposed goal's quoted name was cut).
                    kept = re.sub(r"^(?:and|but|so|then)\s+", "", " ".join(good).strip().rstrip(",;:"), flags=re.I)
                    kept = kept[:1].upper() + kept[1:]
                    u.text = kept if re.search(r"[.!?][\"\u201d)]?$", kept) else kept + "."
                else:
                    u.kept = False
                    continue
            if "?" in u.text and standard.max_questions is not None and not u.protected:
                asked += u.text.count("?")
                if asked > standard.max_questions:
                    u.kept = False
        if not any(u.kept for u in units):
            return draft
        if trimmed_figure:
            relation = computed_sentence(draft, evidence_index)
            if relation:
                units.append(Unit(relation))
        if standard.max_sentences is not None:
            units = shorten(units, standard.max_sentences, question, " ".join(facts or []))
        units, _ = cohere(units, question)      # a reference whose sentence the repair removed is not left pointing at nothing
        fixed = joined(units) or draft
        if standard.forbid_markup:      # the last resort for a reply read aloud: the marks go, the words stay
            fixed = re.sub(r"[*#`_>]|[\"“”]", "", fixed)
        return fixed
    except Exception:  # noqa: BLE001
        return draft


def repair_limit(cfg: Any, standard: Standard) -> int:
    """How many regeneration rounds a reply may spend on its measurements: an information, settled or spoken reply is rewritten once (each rewrite is a
    full model call on the person's wait), the other situations keep the configured number. ``cheap_information_turns`` False keeps it for all."""
    configured = int(getattr(cfg, "reply_standard_max_repairs", 2))
    if getattr(cfg, "cheap_information_turns", True) and standard.shape in (SHAPE_INFORMATION, SHAPE_SPOKEN, SHAPE_SETTLED):
        return min(configured, 1)
    return configured


def steering(hint: str, previous: str) -> str:
    """The text a regeneration is steered with: the reviewer's hint and the answer it concerns."""
    return f"A reviewer flagged this for one correction: {hint}\n\n--- YOUR PREVIOUS ANSWER ---\n{previous}"


POINTED_NAME = re.compile(r"[\"“]([^\"”\n]{3,120})[\"”]")


def pointed_items(text: str) -> frozenset:
    """What a reply points the person to: its web addresses and the names it quotes (a goal's or workflow's exact title). A repair for length must keep them: they are
    where the person finds what the reply is about. Compares text; reads no meaning."""
    from quest_ai_runner.the_intelligence.critical_thinking.response_quality.link_provenance_pass import http_addresses
    return http_addresses(text) | frozenset(m.group(1).strip() for m in POINTED_NAME.finditer(text or ""))


def lost_pointers(candidate: str, held: Any) -> frozenset:
    """The items of ``held`` the candidate no longer holds (an address by its normalised form, a quoted name by its text)."""
    from quest_ai_runner.the_intelligence.critical_thinking.response_quality.link_provenance_pass import http_addresses
    present = http_addresses(candidate)
    return frozenset(item for item in (held or ()) if item not in present and item not in (candidate or ""))


def keep_addresses_hint(draft: str, held: Any) -> str:
    """The sentence added to a repair hint when the draft holds web addresses or quoted names: they must survive the repair (the length cap yields)."""
    if not held:
        return ""
    return " Keep every web address and every quoted name in the answer exactly as written, even if that leaves it longer than the limit."
