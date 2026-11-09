"""One measurable AI increment on a quest's milestone, for a turn whose resolved intent is progress on it ("get X moving"): the flow step that replaced the
owned-next-step write.

Role: QAR's wiring of the hub's ``CriticalThinkingComposition.progress`` for the milestones a quest keeps in Quest. A milestone is never waiting on a person:
the turn always makes something the AI can do itself and shows it measured before and after. The steps, each decided on structured fields:

1. **Work items.** One cheap call decomposes the milestone's Solution record into work items (``kind``, ``actor``, ``inputs`` as record ids, ``outward`` when
   the item publishes, sends or pays, ``moves_parent``, ``requirements``); a second, independent call says for each whether the AI can do it now with reading
   and writing only. An item is the AI's only when both agree and code finds its inputs and no outward act (the hub's ``doable_by_ai``).
2. **Metric upkeep** (hub ``metric-upkeep``): when every item that moves the milestone's own metric is a person's, one intermediate metric the AI can move is
   added through ``apply_spec_edit``, after Decision Check agrees it tracks the done test.
3. **The increment** (hub ``choose_increment``), measured BEFORE on what the milestone's keyed deliverable goal already holds (nothing scores 0), then made by
   one writing call grounded in the turn's reads (each claim checked by ``grounded_text``), then measured AFTER by a scoring call that is not the writer
   (a requirement counts as met only with a quote code finds in the part).
4. **Kept** with ``keep_milestone_deliverable`` on the keyed goal ``<milestone> deliverables`` (updated in place on a repeat), the increment's readings in the
   call, and the milestone's metric **read again** through ``read_spec_records``.

The reply is composed by code from those results: the deliverable, the increment before and after, the milestone metric read again, then at most ONE step only
a person can do. A failure to read or write is a ``LaneGap`` with the tool's own words, never an empty result.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("quest-ai-runner.milestone-progress")

KEEP_TOOL = "keep_milestone_deliverable"
SPEC_EDIT_TOOL = "apply_spec_edit"
OUTWARD = "outward_act"
DELIVERABLES = "deliverables"
PART_SEPARATOR = "\n\n--- Part {n} ---\n"
PART_SPLIT = re.compile(r"\n*--- Part \d+ ---\n")
OVERVIEW_HEAD = "Overview:\n"
GROUNDING_CHARS = 12000
DEPTH_CALLS = 16       # the depth loop's call budget for one unit (work and check calls; the hub default)
RECORD_CHARS = 1500
NO_EM_DASH = "Do not use em dashes."


#: The deliverable's rubric is durable knowledge on the milestone's Solution record (its ``knowledge`` field, one entry per line, the ``[kind] claim (source: ...)``
#: shape the ESOI knowledge records migrate from), written once through ESOI Application and reused by the writer and the scorer on every later turn.
RUBRIC_KIND = "best guess"
RUBRIC_CLAIM = "Deliverable rubric for "
RUBRIC_LINE = re.compile(r"^\[" + RUBRIC_KIND + r"\] " + RUBRIC_CLAIM + r"(?P<item>[^:]+?)(?: \((?P<total>\d+) units\))?: (?P<rubric>.+) \(source: (?P<source>[^)]*)\)\s*$")
TO_BE_DECIDED = "to be decided"


@dataclass(frozen=True)
class KeptRubric:
    """A deliverable rubric as the milestone's record keeps it."""
    item: str
    requirements: List[str]
    total: Optional[int]
    source: str


def kept_rubric(row: Mapping[str, Any]) -> Optional[KeptRubric]:
    """The deliverable rubric the milestone's record keeps (the last one written), else None. Reads only the line shape this module writes."""
    found = None
    for line in str(row.get("knowledge") or "").splitlines():
        match = RUBRIC_LINE.match(line.strip())
        if match:
            found = KeptRubric(match.group("item").strip(), [r.strip() for r in match.group("rubric").split(";") if r.strip()],
                               int(match.group("total")) if match.group("total") else None, match.group("source").strip())
    return found


def rubric_entry(item: str, requirements: Sequence[str], total: Optional[int], source: str) -> str:
    """One knowledge line for a rubric: the kind in brackets, the claim (item, unit count and the requirements joined by "; "), and its source."""
    clean = lambda text: " ".join(str(text).replace(";", ",").replace(":", ",").replace("(", "").replace(")", "").split())      # noqa: E731
    count = f" ({total} units)" if total and total > 1 else ""
    return f"[{RUBRIC_KIND}] {RUBRIC_CLAIM}{clean(item)}{count}: " + "; ".join(clean(r) for r in requirements if str(r).strip()) + f" (source: {source})"


def with_entry(knowledge: Any, entry: str) -> str:
    """The knowledge text with ``entry`` as its rubric line: an older rubric line is replaced, never kept beside it."""
    kept = [line for line in str(knowledge or "").splitlines() if line.strip() and not RUBRIC_LINE.match(line.strip())]
    return "\n".join(kept + [entry])


class ToolSpecEdit:
    """ESOI Application at the turn's ``apply_spec_edit`` tool: the hub's upkeep writes the intermediate metric through it, under Quest's own approval rules."""

    def __init__(self, registry: Any, quest_id: str):
        self.registry, self.quest_id = registry, quest_id

    def read(self, subject: str = "") -> Mapping[str, Any]:
        return {}

    def apply(self, change: Mapping[str, Any]) -> Mapping[str, Any]:
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if self.registry is None or self.registry.get(SPEC_EDIT_TOOL) is None:
            return {"applied": False, "reason": f"this lane has no {SPEC_EDIT_TOOL} tool"}
        edit = dict(change.get("edit") or change)
        if edit.get("kind") == "record_add" and edit.get("table") == "metrics":
            edit["fields"] = quest_metric_fields(edit.get("fields") or {})
        result = self.registry.invoke(SPEC_EDIT_TOOL, {"quest_id": self.quest_id, "change": edit}, ToolContext(quest_id=self.quest_id))
        data = dict(getattr(result, "data", None) or {}) if isinstance(getattr(result, "data", None), Mapping) else {}
        if getattr(result, "ok", False):
            return dict({"applied": True}, **data)
        return dict({"applied": False, "reason": str(getattr(result, "text", ""))[:300]}, **data)


#: The columns Quest keeps on a metric record that a new one may set (``parent_metric_ids`` is retired there: the link to the parent is the
#: ``cascading_conditionals`` line, which states it).
QUEST_METRIC_COLUMNS = ("name", "purpose", "record", "cascading_conditionals", "solution_ids")


def quest_metric_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """An intermediate metric's fields as Quest stores them: its own columns, with what is counted (the unit, the total and what each unit must meet) said in
    its purpose, so a person reading the record sees the definition whole."""
    out = {k: v for k, v in fields.items() if k in QUEST_METRIC_COLUMNS}
    rubric = [str(r) for r in fields.get("rubric") or [] if str(r).strip()]
    extra = (f" Counted out of {fields['total']} {fields.get('unit') or 'units'}." if fields.get("total") else "") + (
        " Each counts when it: " + "; ".join(rubric) + "." if rubric else "")
    if extra:
        out["purpose"] = (str(out.get("purpose") or "").rstrip() + extra).strip()
    return out


def parse_json(text: Any) -> Dict[str, Any]:
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def squash(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


DECOMPOSE_PROMPT = """A team's milestone, from its own records:
{records}

Break the work this milestone needs into at most 6 work items, in the order they would be done. Reply with JSON only:
{{"items": [{{"name": "<what is made or done, short>", "kind": "deliverable" | "evidence" | "recommendation" | "outward",
"actor": "ai" | "person", "outward": <true when the item publishes, sends, pays, records on camera, submits to a store or contacts someone>,
"moves_parent": <true when doing it directly moves the milestone's own metric>, "done_material": <true for the one item whose output the done
test itself is made of or published from (for published videos, their scripts; for a decision, the analysis that makes it)>, "inputs": [<record ids from above it needs>],
"total": <how many units the milestone needs of this item, a whole number, or 1>, "requirements": ["<what one unit must meet to count>", ...],
"step": "<for an item only a person can do: one full sentence saying what is done and, when the records say, by whom>"}}]}}
A deliverable is a piece of the very thing the done test needs made, written now from the records: the content that will be published, sent, submitted or
used (the words of a script, listing copy, a message, a question set, an analysis). A plan, a status report, a schedule, or a list of owners or dates is
bookkeeping, never a deliverable. A comparison of options, a recommendation or a proposed list (topics, candidates) that a decision needs is a deliverable the AI
proposes now, labelled as a proposal with its reasons and sources; it is never left for later. Evidence is answering the questions a decision needs from data or
sources; actor is person for anything only a person can do. Give 3 or 4 requirements for each item an AI can make, each
checkable by reading one unit's own text against the records (never by what happens later, outside the text, or after a person acts), and asking only for
what can be written from the records: never a title, screen, shot, length, figure or date the records do not describe, and never one that waits on a
decision people have not made yet (an agreed list, an approval). For anything that speaks
about a product, a team or past work, one requirement is that every claim in it is one the records or the reads state.
Use only what the records say; invent no figure, name or date. {no_em_dash}"""

VERIFY_PROMPT = """Work items for a milestone:
{items}

For each, judge on its own: can an AI do it right now with nothing but reading the team's records and writing text (no publishing, sending, paying, recording,
submitting or contacting anyone)? Reply with JSON only: {{"verdicts": [{{"n": <number>, "ai_can": true | false}}]}}."""

WRITE_PROMPT = """You write one deliverable for a team's milestone. It is used as it is, so write the thing itself, never a plan to write it.

The milestone, from its records:
{records}

What this turn read and was shown, and the research gathered for this work (each source named; the only source for any fact about the product, the team or
past work):
{grounding}

The work item: {item}
Each unit must meet: {requirements}
{kept}
Reply with JSON only: {{"units": [{{"n": <number>, "topic": "<what that unit covers>", "why": "<the reason, from the sources>", "source": "<the source it rests on>"}}],
"part": "<the next unit, complete, meeting every requirement above>"}}. List every unit of the set in "units" (as many as the set needs); "part" is the next one
in full. The part is the unit itself, used as it is: for a video, the words to be spoken; for copy, the copy; for a message, the message; never notes, an outline or
a plan about the unit unless the unit is itself an outline.
Facts and proposals are different. A fact (what the product does, a figure, a date, a name, a result) must come from the sources above, said as they say it;
never invent one. A proposal (which topics to cover, which option to recommend, how to compare candidates) is your own work: make it, concretely, from the
sources, and say why. When the work compares options or prepares a decision, end with an explicit recommendation of one option, labelled as a proposal,
with its reasoning, even when the evidence is thin (say how thin). Write "to be decided" only for a fact that only a person or an outside system can supply,
never for a proposal you can make. No first
person plural unless the records say who speaks; no bracket or blank slot to fill. {no_em_dash}"""

EVIDENCE_PROMPT = """You gather the evidence a team's decision needs. Answer only from the records and the reads below; quote the exact words each answer rests on.

The milestone, from its records:
{records}

What this turn read and was shown:
{grounding}

The work item: {item}
Each answer must meet: {requirements}

List the questions this decision needs answered (at most {limit}), and answer each one the records or the reads can answer. Reply with JSON only:
{{"questions": [{{"question": "<one full sentence>", "answer": "<the answer, or empty when the records and reads do not give it>",
"source": "<the record or read it comes from, by name>", "quote": "<the exact words of that record or read the answer rests on, a few words, empty when unanswered>"}}]}}.
Invent no figure, name or date. {no_em_dash}"""

PART_PROMPT = """One unit of a team's deliverable misses one requirement. Write ONLY the few sentences that meet it, to be joined into the unit; do not
rewrite the unit.

The unit: {goal}
The unit as it stands:
{base}

The requirement it misses: {requirement}

The milestone's records and this turn's reads, the only source for any fact:
{records}
{grounding}

Use only facts above; a proposal (a topic, a recommendation) is yours to make from them, with its reason. Write "to be decided" only for a fact only a
person or an outside system can supply. Reply with JSON only: {{"part": "<the sentences>"}}. {no_em_dash}"""

SCORE_PROMPT = """Check one deliverable against its requirements. You did not write it; judge only what the text shows, against the records below.

The records and reads it may rest on (for a requirement about what the records say):
{reference}

The text:
{part}

Requirements:
{requirements}

For each requirement, say whether the text meets it, and quote the words of the text that show it (an exact copy of a few words). Reply with JSON only:
{{"verdicts": [{{"n": <number>, "meets": true | false, "quote": "<exact words from the text, empty when it does not meet it>"}}]}}."""


@dataclass
class Progress:
    """What the progress step did, for the reply and the turn's record."""
    title: str = ""
    item: str = ""
    overview: str = ""
    parts: List[str] = field(default_factory=list)
    measure_name: str = ""
    unit: str = ""
    before: Optional[float] = None
    after: Optional[float] = None
    total: Optional[int] = None
    as_of: str = ""
    kept_state: str = ""
    metric_name: str = ""
    metric_before: str = ""
    metric_after: str = ""
    ask: str = ""
    child_metric: Optional[Mapping[str, Any]] = None
    status: str = ""
    why: str = ""
    rubric_state: str = ""          # "reused" (kept on the record and read back), "kept" (defined this turn and written), or why it was not kept
    notes: str = ""                 # lines the milestone's parent and dependent solution passes add, after the person-only step
    depth: Optional[Mapping[str, Any]] = None       # the depth loop's account: rubric score of the one-call unit (before) and after depth, calls, levels, why it stopped
    sources: str = ""               # what the writer was given beyond the turn's reads (the research it gathered), kept in the turn's read record
    tier: str = ""                  # the tier the deliverable was written on
    unsupported: List[str] = field(default_factory=list)     # sentences taken out because they stated a fact no source holds

    def body(self) -> str:
        """The deliverable as kept on the goal: the overview, then each part under its separator."""
        out = (OVERVIEW_HEAD + self.overview.strip()) if self.overview.strip() else ""
        for n, part in enumerate(self.parts, 1):
            out += PART_SEPARATOR.format(n=n) + part.strip()
        return out.strip()

    def increment_line(self) -> str:
        if self.before is None or self.after is None:
            return ""
        shown = lambda v: str(int(v)) if float(v).is_integer() else f"{v:g}"     # noqa: E731
        of = f" of {self.total}" if self.total else ""
        # The depth loop's own scores stay in the kept increment's record (``depth``), not in the reply: a figure no read of the turn holds reads as invented.
        return f"{self.measure_name}: {shown(self.before)}{of} before, {shown(self.after)}{of} after (scored {self.as_of} by a check separate from the writer)."

    def metric_line(self) -> str:
        if not self.metric_name:
            return ""
        if self.metric_after and self.metric_after == self.metric_before:
            return f"Milestone metric ({self.metric_name}), read again after the work: unchanged at {self.metric_after}, because it moves when a person does the step below."
        if self.metric_after:
            return f"Milestone metric ({self.metric_name}), read again after the work: was {self.metric_before or 'not recorded'}, now {self.metric_after}."
        return f"Milestone metric ({self.metric_name}): {self.metric_before or 'not recorded'}; it could not be read again just now."

    def reply_text(self) -> str:
        """The reply, composed by code: the deliverable first, then the measured increment, the milestone metric read again, then the one person-only step."""
        item = self.item.rstrip(".")
        item = item[0].lower() + item[1:] if item[:2] != item[:2].upper() else item          # "Scripts for ..." reads "scripts for ..." mid-sentence; an acronym stays
        what = f"{item} (part {len(self.parts)}" + (f" of {self.total})" if self.total else ")") if self.parts else item
        lead = {"created": f"I wrote {what} and kept it on the goal '{self.title}'.",
                "updated": f"I wrote {what} and kept it on the goal '{self.title}', updated in place (no second goal).",
                "unchanged": f"The goal '{self.title}' already holds this work, so nothing new was kept."}.get(self.kept_state, f"I wrote {what}.")
        parts = [lead, self.body()]
        tail = " ".join(x for x in (self.increment_line(), self.metric_line()) if x)
        if tail:
            parts.append(tail)
        if self.ask:
            parts.append(f"The one step only a person can do: {self.ask.rstrip('.')}.")
        if self.notes:
            parts.append(self.notes)
        return "\n\n".join(p for p in parts if p.strip())


SUPPORT_PROMPT = """Classify each numbered sentence of a team's deliverable.

The sources (records, what the turn read, research gathered):
{reference}

The sentences:
{sentences}

For each sentence, say its kind: "fact" (it states something about the product, a team, people, figures, dates, places or past results), "proposal" (it
proposes, recommends, compares or plans, and is worded as such), or "plain" (a greeting, a call to action, a transition, or "to be decided"). For a fact,
quote the exact words of the sources it rests on (a few words, copied exactly), or leave the quote empty when no source states it. Reply with JSON only:
{{"sentences": [{{"n": <number>, "kind": "fact" | "proposal" | "plain", "quote": "<exact words from the sources, or empty>"}}]}}."""

SUPPORT_VIEWS = 2       # independent classifications; a sentence one of them finds to be an unsupported fact is taken out


def supported_sentences(text: str, reference: str, decide: Callable[[str], str], keep_line_starts: bool = False) -> Tuple[str, List[str]]:
    """``(text, removed)``: every sentence that states a fact must rest on words code finds in ``reference`` (the records, the turn's reads and the research);
    one that does not is taken out. Two independent classifications (the second sees the sentences in reverse order); a sentence either finds to be a fact
    with no quote in the sources is removed, so a missed invention needs both to miss it. Proposals and plain sentences stay. The structure, not the
    wording, decides: a quote is checked by code. With ``keep_line_starts`` the first sentence of each line (a proposed item's own name) is never taken out;
    the sentences that argue for it are checked like any other."""
    from hubofhubs.api import joined, units_of
    units = units_of(text)
    starts = {k for k, u in enumerate(units) if keep_line_starts and (k == 0 or "\n" in (u.lead or ""))}
    # A list number ("1.") is split off as its own sentence: the sentence after it is the item's name.
    starts |= {k + 1 for k in list(starts) if k + 1 < len(units) and re.fullmatch(r"\d+[.)]", units[k].text.strip())}
    if not units:
        return text, []
    source = squash(reference)
    order = list(range(len(units)))
    unsupported: set = set()
    for view in range(SUPPORT_VIEWS):
        seen = order if view % 2 == 0 else order[::-1]
        listing = "\n".join(f"{n}. {units[k].text}" for n, k in enumerate(seen, 1))
        data = parse_json(decide(SUPPORT_PROMPT.format(reference=str(reference)[:GROUNDING_CHARS * 2], sentences=listing)))
        for row in data.get("sentences") or []:
            try:
                k = seen[int(row.get("n")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            quote = squash(row.get("quote"))
            if row.get("kind") == "fact" and not (len(quote.split()) >= 3 and quote in source):
                unsupported.add(k)
    unsupported -= starts
    removed = [units[k].text for k in sorted(unsupported)]
    for k in unsupported:
        if sum(u.kept for u in units) > 1:
            units[k].kept = False
    return joined(units), removed


class Scorer:
    """The judge the hub's ``measure`` calls: one scoring call per part (cached), a requirement met only when its quote is found in the part."""

    def __init__(self, decide: Callable[[str], str], requirements: Sequence[str], reference: Any = ""):
        self.decide, self.requirements, self.cache, self.reference = decide, [str(r) for r in requirements], {}, reference      # text, or a callable read at scoring time

    VIEWS = 3       # independent scoring calls (alternate ones see the requirements in reverse order); a requirement is met when most of them find it, each with a quote

    def one_view(self, part: str, order: List[int]) -> List[bool]:
        listing = "\n".join(f"{n}. {self.requirements[k]}" for n, k in enumerate(order, 1))
        reference = str(self.reference() if callable(self.reference) else self.reference)
        data = parse_json(self.decide(SCORE_PROMPT.format(part=part[:4000], requirements=listing, reference=reference[:GROUNDING_CHARS * 2] or "(none)")))
        met = [False] * len(self.requirements)
        text = squash(part)
        for row in data.get("verdicts") or []:
            try:
                n = int(row.get("n")) - 1
            except (TypeError, ValueError, AttributeError):
                continue
            quote = squash(row.get("quote"))
            if 0 <= n < len(order) and row.get("meets") is True and len(quote) >= 3 and quote in text:
                met[order[n]] = True
        return met

    def verdicts(self, part: str) -> List[bool]:
        key = hashlib.sha1(squash(part).encode("utf-8")).hexdigest()      # the same words are one verdict, before and after alike
        if key not in self.cache:
            forward = list(range(len(self.requirements)))
            views = [self.one_view(part, forward if v % 2 == 0 else forward[::-1]) for v in range(self.VIEWS)]
            self.cache[key] = [2 * sum(view[k] for view in views) > len(views) for k in forward]
        return self.cache[key]

    def __call__(self, requirement: Optional[str], part: str) -> bool:
        if requirement not in self.requirements:
            return False
        return self.verdicts(part)[self.requirements.index(requirement)]


def kept_parts(text: str) -> List[str]:
    """The parts a kept deliverable goal holds (after its overview), in order; [] for none."""
    body = str(text or "")
    pieces = PART_SPLIT.split(body)
    return [p.strip() for p in pieces[1:] if p.strip()]


def work_items(records: str, rids: Sequence[str], decide: Callable[[str], str]) -> List[Dict[str, Any]]:
    """The milestone's work items with both independent checks recorded (``checks``) and the outward ones marked with the outward means (``tools``)."""
    data = parse_json(decide(DECOMPOSE_PROMPT.format(records=records[:RECORD_CHARS * 2], no_em_dash=NO_EM_DASH)))
    items = [dict(i) for i in data.get("items") or [] if isinstance(i, Mapping) and str(i.get("name") or "").strip()][:6]
    if not items:
        return []
    listing = "\n".join(f"{n}. {i['name']}" for n, i in enumerate(items, 1))
    verdicts = {int(v.get("n")): v.get("ai_can") is True for v in (parse_json(decide(VERIFY_PROMPT.format(items=listing))).get("verdicts") or [])
                if isinstance(v, Mapping) and str(v.get("n") or "").isdigit()}
    known = set(rids)
    for n, item in enumerate(items, 1):
        item["kind"] = str(item.get("kind") or "")
        item["inputs"] = [str(x) for x in item.get("inputs") or [] if str(x) in known]       # an input that names no record is dropped, never trusted
        item["tools"] = [OUTWARD] if item.get("outward") is True or item["kind"] == "outward" else []
        item["checks"] = [str(item.get("actor") or "") == "ai", verdicts.get(n, False)]
        item["requirements"] = [str(r) for r in item.get("requirements") or [] if str(r).strip()][:5]
    # The item the done test is made from goes first, so the increment rule (which takes the first AI-doable deliverable) makes that, not a side piece.
    return sorted(items, key=lambda i: i.get("done_material") is not True)


def first_person_step(items: Sequence[Mapping[str, Any]], doable: Callable[[Mapping[str, Any]], bool]) -> str:
    """The one step only a person can do: the first item that is not the AI's (outward first)."""
    theirs = [i for i in items if not doable(i)]
    theirs.sort(key=lambda i: not (i.get("tools") and OUTWARD in i["tools"]))
    return " ".join(str(theirs[0].get("step") or theirs[0]["name"]).split()) if theirs else ""


def make_progress(cue: Any, registry: Any, decide: Callable[[str], str], grounding: str = "", today: Optional[datetime.date] = None,
                  write: Optional[Callable[[str], str]] = None, research: Optional[Callable[[str], str]] = None, tier: str = "") -> Progress:
    """The progress step for a resolved milestone ``cue`` (``milestone_cue.Resolved``). Raises ``milestone_cue.LaneGap`` when a read or the keep fails.
    ``decide`` is the cheap call for the structured judgments (work items, checks, scoring); ``write`` writes the deliverable's prose (the tier chosen for
    it, ``tier`` names it; ``decide`` when none); ``research(query) -> text`` reads the product's own documents and outside sources for the facts a
    deliverable needs (each passage with its source)."""
    write = write or decide
    from hubofhubs.api import CriticalThinkingComposition, MajorityVote, RecordFieldUpkeep, doable_by_ai
    from quest_ai_runner.the_intelligence.critical_thinking.decision_check.reply_views import grounded_text
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    from quest_ai_runner.the_world.tools.tools import ToolContext

    # The day in the quest's own time zone (the week the cue was resolved in), so a date in the reply is the person's today.
    day = (today or milestone_cue.today_in(getattr(cue.week, "zone", "") or "")).isoformat()
    cue.read_source(registry)       # the milestone metric as its source reads now (the host's recorded value first); a source that cannot be read is said, never guessed
    tables = cue.tables or {}
    metrics = {mid: with_reading_fields(m) for mid, m in dict(tables.get("metrics") or {}).items()}
    rids = [cue.rid, *cue.metric_ids]
    items = work_items(cue.records_text, rids, decide)
    means = [{"name": OUTWARD, "outward": True}]
    doable = lambda item: doable_by_ai(item, means, rids)      # noqa: E731
    spec_edit = ToolSpecEdit(registry, cue.quest_id)
    row = whole_row(registry, cue)
    rubric = kept_rubric(row)
    rubric_state = ""
    knowledge_now = str(row.get("knowledge") or "")
    if rubric is not None:
        # The record keeps the deliverable and its rubric from an earlier turn: that is the work item now, for the writer and the scorer alike. It was agreed when it
        # was defined (both checks), so it is not judged again; the work items of this turn only give the person-only step and what moves the metric.
        others = [i for i in items if not (str(i.get("kind") or "") == "deliverable" and doable(i))]
        items = [{"name": rubric.item, "kind": "deliverable", "actor": "ai", "outward": False, "moves_parent": False, "inputs": [cue.rid], "tools": [],
                  "checks": [True, True], "total": rubric.total or 1, "requirements": list(rubric.requirements), "kept_rubric": True}] + others
        rubric_state = "reused"
    else:
        items = confirmed_item_order(items, doable, cue.records_text, decide)
        first = next((i for i in items if str(i.get("kind") or "") == "deliverable" and doable(i) and i.get("requirements")), None)
        if first is not None:
            # Defined once, with depth: what a good deliverable of this kind is, researched from the product's documents, cited sources and the done test,
            # agreed by a second view, and kept on the milestone's own record through ESOI Application (the same gate and autopilot rules as any spec edit),
            # so every later turn reuses it instead of drifting.
            researched, cited = researched_rubric(first, cue, research, decide)
            if researched:
                first["requirements"] = researched
            entry = rubric_entry(first["name"], first["requirements"], first.get("total") if isinstance(first.get("total"), int) else None,
                                 ", ".join(cited + [f"{cue.rid}", "Quest AI", day]))
            stored = kept_rubric({"knowledge": entry})
            first["requirements"] = stored.requirements if stored else first["requirements"]     # the words as stored, so a later turn's reuse scores the same rubric
            answer = spec_edit.apply({"edit": {"kind": "spec_edit", "target": cue.rid, "field": "knowledge", "value": with_entry(row.get("knowledge"), entry)}})
            if answer.get("applied"):
                knowledge_now = with_entry(row.get("knowledge"), entry)
            rubric_state = "kept" if answer.get("applied") else ("proposed" if answer.get("proposed") else "not kept: " + str(answer.get("reason") or "refused")[:200])
    moved_by = "person" if [i for i in items if i.get("moves_parent")] and not any(doable(i) for i in items if i.get("moves_parent")) else "ai"
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
    # The milestone's keyed goal is the one Quest AI already keeps for it (linked to the milestone's record), updated in place; only a milestone with none gets a new one.
    kept_goal = next((g for g in cue.candidates if str(g.get("spec_solution_id") or "") == cue.rid and is_owned(g)), None)
    title = str((kept_goal or {}).get("title") or (kept_goal or {}).get("name") or f"{cue.name} {DELIVERABLES}").strip()
    kept_text = str((kept_goal or {}).get("text") or (kept_goal or {}).get("description") or "")
    # What the goal already holds, measured again for the before: its parts for a rubric, its answered questions for evidence (nothing kept scores 0).
    kept = dict({"parts": kept_parts(kept_text)}, **(questions_of(kept_text) or {})) if kept_text else None
    milestone = {"solution_id": cue.rid, "name": cue.name, "done_when": cue.done_when, "moved_by": moved_by, "records": rids,
                 "lands_on": {"collection": "goals", "title_prefix": cue.name}, "knowledge": knowledge_now, "today": day,
                 "sources": [cue.source] if cue.source else []}
    decision = MajorityVote(plumbing=None)
    composition = CriticalThinkingComposition(None, spec_edit, None, None, decision, RecordFieldUpkeep(esoi=spec_edit, decision=decision))
    out = Progress(title=title, metric_name=str((metrics.get(cue.metric_ids[0]) or {}).get("name") or "") if cue.metric_ids else "",
                   metric_before=cue.current_text() if cue.reading else cue.value, as_of=day, rubric_state=rubric_state)
    source = cue.source
    def unreadable(mid: str) -> Mapping[str, Any]:
        raise RuntimeError(cue.reading_gap or "the source could not be read")
    # The source as read this turn (Metric Upkeep compares its day); a source this environment could not read is said so, and upkeep proposes how it becomes readable.
    readers = ({source: (lambda mid: dict(cue.reading))} if cue.reading and str((cue.reading.get("detail") or {}).get("from") or "") != "the metric record"
               else {source: unreadable} if cue.reading_gap else None) if source else None
    chosen: Dict[str, Any] = {}

    scorers: Dict[tuple, Scorer] = {}

    def judge(requirement: Optional[str], part: str) -> bool:
        # The requirements a requirement belongs to are its work item's: one scoring call covers all of them for a part, before and after alike.
        owner = tuple(chosen.get("increment", {}).get("requirements") or ()) if requirement in (chosen.get("increment", {}).get("requirements") or ()) else next(
            (tuple(i["requirements"]) for i in items if requirement in (i.get("requirements") or [])), (str(requirement),))
        scorer = scorers.setdefault(owner, Scorer(decide, owner, sources_text))
        return scorer(requirement, part)

    found: Dict[str, str] = {}

    def gather_research(item: Mapping[str, Any]) -> str:
        """The research piece: the product's own documents and outside sources on what this work is about, each passage with its source, read once per
        turn. What it returns counts as a read for the writer, the claim check and the scorer alike."""
        if research is None or "text" in found:
            return found.get("text", "")
        try:
            found["text"] = str(research(f"{item.get('name')}. {cue.name}. {cue.purpose}"[:600]) or "").strip()
        except Exception as error:  # noqa: BLE001  research that could not run leaves the records and reads as the only sources
            log.warning("milestone progress: research failed: %s: %s", type(error).__name__, error)
            found["text"] = ""
        out.sources = found["text"]
        return found["text"]

    def sources_text() -> str:
        return "\n".join(t for t in (cue.records_text, cue.allowed_text(), str(grounding or ""), found.get("text", "")) if t)

    def shown() -> str:
        return ("\n\n".join(t for t in (str(grounding or "")[:GROUNDING_CHARS], ("Research gathered for this work:\n" + found["text"][:GROUNDING_CHARS]) if found.get("text") else "") if t)
                or "(none)")

    def work(increment: Mapping[str, Any]) -> Mapping[str, Any]:
        item = dict(increment.get("item") or {})
        chosen.update(item=item, increment=dict(increment))
        if increment.get("method") == "read":
            reading = dict(item.get("reading") or {})
            out.parts = [f"{milestone_cue.CURRENT_VALUE} {reading.get('value')} (as of {reading.get('as_of')}, from {reading.get('source') or cue.source})"]
            out.overview = f"{out.metric_name or cue.name}, read from its source because the record was behind it."
            return {"artifact": {"value": reading.get("value")}, "evidence_ref": title}
        gather_research(item)
        if increment.get("method") == "evidence":
            return gather_evidence(item, increment, kept)
        total = item.get("total") if isinstance(item.get("total"), int) else None
        have = list((kept or {}).get("parts") or [])
        kept_note = (f"Already kept (do not repeat; write the next unit, number {len(have) + 1}):\n" + "\n".join(f"- {clip_line(p)}" for p in have)) if have else ""
        data = parse_json(write(WRITE_PROMPT.format(records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(),
                                                    item=item.get("name"), requirements="; ".join(increment.get("requirements") or []) or "(none given)",
                                                    kept=kept_note, total=total or "the", no_em_dash=NO_EM_DASH)))
        allowed = sources_text()
        part = str(data.get("part") or "").strip()
        requirements = [str(r) for r in increment.get("requirements") or []]
        if part and requirements:
            # The unit is its own solution with the kept rubric as its metric: the hub's depth loop checks it (the separate scorer, never the writer), splits what
            # it misses into sub-pieces with their own sub-prompts, recombines and checks again within the request's budget.
            from hubofhubs.api import build_out
            prompt = WRITE_PROMPT.format(records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(),
                                         item=item.get("name"), requirements="; ".join(requirements), kept=kept_note, total=total or "the", no_em_dash=NO_EM_DASH)
            unit = {"goal": f"{item.get('name')}, unit {len(have) + 1}", "requirements": requirements, "prompt": prompt, "solution_id": cue.rid}

            def piece_work(piece: Mapping[str, Any]) -> str:
                answer = write(str(piece.get("prompt") or ""))
                return str(parse_json(answer).get("part") or answer or "").strip()

            def piece_check(text: str, piece: Mapping[str, Any]) -> Dict[str, Any]:
                wanted = [str(r) for r in piece.get("requirements") or []]
                met = [r for r in wanted if judge(r, text)] if text.strip() else []
                missing = [r for r in wanted if r not in met]
                return {"met": not missing, "score": len(met) / len(wanted) if wanted else 0.0, "missing": missing,
                        "reason": "meets every requirement" if not missing else "misses: " + "; ".join(missing)}

            def piece_split(piece: Mapping[str, Any], checked: Mapping[str, Any]) -> List[Dict[str, Any]]:
                # One sub-piece per requirement the check found missing, each asked for only the sentences that meet it (the parent's prompt asks for a
                # whole unit, which made every sub-piece another whole version).
                missing = [str(r) for r in checked.get("missing") or []] or list(piece.get("requirements") or [])
                return [{"goal": f"{piece.get('goal')}: {req}", "requirements": [req], "solution_id": cue.rid,
                         "prompt": PART_PROMPT.format(goal=piece.get("goal"), base=part, requirement=req, records=cue.records_text[:RECORD_CHARS * 2],
                                                      grounding=shown(), no_em_dash=NO_EM_DASH)} for req in missing]

            def piece_combine(piece: Mapping[str, Any], base: str, parts: Sequence[Mapping[str, Any]]) -> str:
                # The parts' own sentences are kept exactly, after the unit as it stood: a model rewrite of the join lost what a part had met (measured: the
                # part met its requirement and the recombined text did not).
                return "\n\n".join([base.strip()] + [str(p["text"]).strip() for p in parts if str(p.get("text") or "").strip()]).strip()
            deep = build_out(unit, piece_work, piece_check, split=piece_split, combine=piece_combine, budget={"calls": DEPTH_CALLS, "depth": 2, "width": 4}, base=part)
            out.depth = {k: deep.get(k) for k in ("before", "after", "calls", "levels", "stopped", "met")}
            if float(deep.get("after") or 0) >= float(deep.get("before") or 0):
                part = str(deep.get("text") or part).strip()       # depth that scored lower than the one-call unit is not kept
        part = grounded_text(part, allowed, decide) if part else ""
        if part:
            part, dropped = supported_sentences(part, allowed, decide)
            out.unsupported += dropped
        # The list of units is a proposal: the writer proposes what each covers, with its reason and source; its facts go through the same claim check.
        units = [u for u in data.get("units") or [] if isinstance(u, Mapping) and str(u.get("topic") or "").strip()]
        proposed = "\n".join(f"{n}. {' '.join(str(u['topic']).split())}. Why: {' '.join(str(u.get('why') or '').split()) or 'see the sources'}"
                             + (f" (source: {' '.join(str(u['source']).split())})" if str(u.get("source") or "").strip() else "") for n, u in enumerate(units, 1))
        head = f"{item.get('name')}" + (f": {total} units, written one per turn, in order." if total and total > 1 else ".")
        if proposed:
            # A proposed topic is a proposal and stays; the facts given as its reasons must rest on a source like any other.
            proposed, dropped = supported_sentences(grounded_text(proposed, allowed, decide), allowed, decide, keep_line_starts=True)
            out.unsupported += dropped
        out.overview = head + (("\nProposed units (an AI proposal from the sources, to be agreed):\n" + proposed) if proposed else "")
        out.parts = have + ([part] if part else [])
        return {"artifact": {"parts": out.parts} if part else None, "evidence_ref": title}

    def gather_evidence(item: Mapping[str, Any], increment: Mapping[str, Any], kept: Any) -> Mapping[str, Any]:
        """Research increment: the questions the decision needs, each answered only with a quote code finds in the records or reads; the rest stay open
        ("to be decided"). Coverage is questions answered with a source out of questions asked."""
        data = parse_json(write(EVIDENCE_PROMPT.format(records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(),
                                                        item=item.get("name"), requirements="; ".join(increment.get("requirements") or []) or "(none given)",
                                                        limit=EVIDENCE_QUESTIONS, no_em_dash=NO_EM_DASH)))
        text = squash(sources_text())
        questions = []
        for row in data.get("questions") or []:
            if not isinstance(row, Mapping) or not str(row.get("question") or "").strip():
                continue
            quote = squash(row.get("quote"))
            grounded = bool(str(row.get("answer") or "").strip()) and bool(str(row.get("source") or "").strip()) and len(quote) >= 3 and quote in text
            questions.append({"question": " ".join(str(row["question"]).split()), "answer": " ".join(str(row.get("answer") or "").split()) if grounded else "",
                              "source": " ".join(str(row.get("source") or "").split()) if grounded else ""})
        questions = questions[:EVIDENCE_QUESTIONS]
        out.parts = [render_questions(questions)] if questions else []
        out.overview = f"The questions the decision needs, each answered from a cited record or read; the rest are {TO_BE_DECIDED}."
        return {"artifact": {"questions": questions} if questions else None, "evidence_ref": title}

    made = composition.progress(milestone, metrics, items, work, judge=judge, means=means, readers=readers, today=day, ask=decide,
                                kept=kept)
    out.status = str(made.get("status") or "")
    increment = dict(made.get("increment") or {})
    item = dict(increment.get("item") or {})
    out.item = str(item.get("name") or "the next deliverable")
    child = next((a.get("child_metric") for a in made.get("actions") or [] if isinstance(a.get("child_metric"), Mapping)), None)
    out.child_metric = child
    out.measure_name = str((child or {}).get("name") or f"Units of {out.item} meeting their requirements")
    out.total = (made.get("after") or made.get("before") or {}).get("total") or (item.get("total") if isinstance(item.get("total"), int) and item.get("total") > 1 else None)
    out.before = (made.get("before") or {}).get("value")
    out.after = (made.get("after") or {}).get("value")
    out.ask = without_ids(first_person_step(items, doable))
    proposal = next((a.get("proposed_source") for a in made.get("actions") or [] if isinstance(a.get("proposed_source"), Mapping)), None)
    if proposal:
        out.notes = (f"To make {out.metric_name or 'the milestone metric'} readable here (an AI proposal, kept on the milestone's record): read "
                     f"{proposal.get('source') or 'a source to be connected'}" + (f", field {proposal['field']}" if proposal.get("field") else "")
                     + f"; a person connects {str(proposal.get('connect') or '').rstrip('.')}.")
    if out.status != "measured":
        out.why = str(made.get("message") or out.status)
        return out
    result = registry.invoke(KEEP_TOOL, {"quest_id": cue.quest_id, "title": title, "body": out.body(), "solution_id": cue.rid,
                                         "increment": {"kind": increment.get("kind"), "measure_id": increment.get("measure_id"), "measure_name": out.measure_name,
                                                       "before": out.before, "after": out.after, "total": out.total, "method": increment.get("method"),
                                                       "as_of": day, "changed": out.after != out.before, "depth": out.depth}}, ToolContext(quest_id=cue.quest_id))
    if not getattr(result, "ok", False):
        raise milestone_cue.LaneGap(f"the deliverable for {cue.name!r} was not kept: {str(getattr(result, 'text', ''))[:300]}")
    out.kept_state = str((result.data or {}).get("state") or "")
    try:
        again, _ = milestone_cue.read_through(registry, cue.quest_id)       # the milestone's metric read again, after the work
        metric = (again.get("metrics") or {}).get(cue.metric_ids[0]) if cue.metric_ids else None
        line = str((metric or {}).get("record") or "").strip().split("\n")[0].strip()
        kept_reading = milestone_cue.Resolved.KEPT_READING.match(line)
        # A dated reading line is said as its value and day (the clip of a whole line would stop at its colon); any other record by its first sentence.
        out.metric_after = (f"{kept_reading.group('value').strip()} (as of {kept_reading.group('day').strip()}, from {kept_reading.group('source').strip()})" if kept_reading
                            else milestone_cue.first_sentence(line)) if metric else ""
    except milestone_cue.LaneGap as gap:
        log.warning("milestone progress: the metric could not be read again: %s", gap)
    return out


EVIDENCE_QUESTIONS = 8

RUBRIC_PROMPT = """Define what a good unit of this deliverable must meet, before it is written.

The deliverable: {item}
The milestone's done test: {done_when}
What the milestone is for: {purpose}

What the product's own documents and outside sources say about this kind of deliverable and this work (each passage under its source):
{research}

Give 3 to 5 requirements a reader can check on one unit's own text: what a good unit of this kind contains and how it is shaped (for a script, for example,
the spoken words, an opening hook, a few concrete points, a call to action). Each requirement must be one the done test or the
sources support; none may ask for a fact the sources do not give or wait on a decision people have not made. Reply with JSON only:
{{"requirements": [{{"text": "<the requirement>", "source": "<the source or the done test it comes from, by name>"}}]}}. Do not use em dashes."""

RUBRIC_CHECK_PROMPT = """These requirements were proposed for a deliverable ({item}), from these sources:
{research}

{listing}

For each requirement, judge on its own: can a reader check it on one unit's text alone, and does the done test ({done_when}) or a source above support it?
Reply with JSON only: {{"verdicts": [{{"n": <number>, "keep": true | false}}]}}."""

ITEM_PICK = "Which of these work items is the one whose output the milestone's done test is itself made of or published from? Pick it."


def researched_rubric(item: Mapping[str, Any], cue: Any, research: Optional[Callable[[str], str]], decide: Callable[[str], str]) -> Tuple[List[str], List[str]]:
    """``(requirements, sources)``: the rubric for a deliverable, from research on what a good one of its kind is (the product's documents, cited sources,
    the done test), each requirement kept only when a second view agrees it is checkable on the text and supported. ``([], [])`` when there is no research
    to define it from, and the work item's own requirements stand."""
    if research is None:
        return [], []
    try:
        found = str(research(f"what makes a good {item.get('name')}; {cue.done_when}"[:600]) or "").strip()
    except Exception as error:  # noqa: BLE001  no research: the work item's own requirements stand
        log.warning("milestone progress: rubric research failed: %s: %s", type(error).__name__, error)
        return [], []
    if not found:
        return [], []
    data = parse_json(decide(RUBRIC_PROMPT.format(item=item.get("name"), done_when=cue.done_when, purpose=clip_line(cue.purpose, 600), research=found[:GROUNDING_CHARS])))
    proposed = [r for r in data.get("requirements") or [] if isinstance(r, Mapping) and str(r.get("text") or "").strip()][:5]
    if not proposed:
        return [], []
    listing = "\n".join(f"{n}. {r['text']}" for n, r in enumerate(proposed, 1))
    verdicts = {int(v.get("n")): v.get("keep") is True for v in parse_json(decide(RUBRIC_CHECK_PROMPT.format(
        item=item.get("name"), research=found[:GROUNDING_CHARS], listing=listing, done_when=cue.done_when))).get("verdicts") or []
        if isinstance(v, Mapping) and str(v.get("n") or "").isdigit()}
    kept = [r for n, r in enumerate(proposed, 1) if verdicts.get(n)]
    if len(kept) < 2:
        return [], []
    sources = list(dict.fromkeys(" ".join(str(r.get("source") or "").replace("(", "").replace(")", "").split()) for r in kept if str(r.get("source") or "").strip()))
    return [" ".join(str(r["text"]).split()) for r in kept], sources[:4]


def confirmed_item_order(items: List[Dict[str, Any]], doable: Callable[[Mapping[str, Any]], bool], records: str, decide: Callable[[str], str]) -> List[Dict[str, Any]]:
    """The work items with the deliverable the done test is made from first, that choice confirmed by Decision Check's independent views (two, a majority) over
    the AI-doable deliverables, the decomposition's own pick as the draft. With one candidate there is nothing to confirm; a check that abstains keeps the draft."""
    candidates = [i for i in items if str(i.get("kind") or "") == "deliverable" and doable(i)]
    if len(candidates) < 2:
        return items
    from hubofhubs.api import MajorityVote
    options = [{"id": f"i{n}", "text": str(c.get("name") or "")} for n, c in enumerate(candidates)]
    evidence = "The milestone's records:\n" + records[:RECORD_CHARS * 2] + "\n\nThe work items:\n" + "\n".join(f"{o['id']}: {o['text']}" for o in options)
    binding = {"kind": "pick", "options": options, "views": [{"id": "done-test", "instruction": ITEM_PICK, "evidence": evidence},
                                                              {"id": "made-from", "instruction": ITEM_PICK + " Read the done test first, then the items.", "evidence": evidence}],
               "max_calls": 3, "parallel": 2}
    try:
        got = MajorityVote(plumbing=None).check("i0", binding, decide)
    except Exception:  # noqa: BLE001  the decomposition's own order stands
        return items
    pick = str(got.get("pick") or "")
    chosen = next((c for o, c in zip(options, candidates) if o["id"] == pick), None)
    if chosen is None:
        return items
    return [chosen] + [i for i in items if i is not chosen]


def with_reading_fields(metric: Mapping[str, Any]) -> Dict[str, Any]:
    """A metric record with the structured fields Metric Upkeep reads, taken from its own dated reading line when it has one (``Current value: <v> (as of
    <day>, from <source>)``): ``value`` (a number when it is one) and ``measured_through`` (that day, unless the record states one)."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    out = dict(metric)
    line = str(metric.get("record") or "").strip().split("\n")[0].strip()
    found = milestone_cue.Resolved.KEPT_READING.match(line)
    if found:
        raw = found.group("value").strip()
        try:
            out.setdefault("value", float(raw.replace(",", "")) if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", raw) else raw)
        except ValueError:
            out.setdefault("value", raw)
        if not str(out.get("measured_through") or "").strip():
            out["measured_through"] = found.group("day").strip()[:10]
    return out


def whole_row(registry: Any, cue: Any) -> Mapping[str, Any]:
    """The milestone's Solution record with its columns whole (the turn's read cuts long values, and the kept rubric line is read back whole); the row the
    cue holds when the lane cannot read it whole."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("read_spec_records") is None:
        return cue.row
    try:
        result = registry.invoke("read_spec_records", {"quest_id": cue.quest_id, "whole_record_ids": [cue.rid]}, ToolContext(quest_id=cue.quest_id))
    except Exception:  # noqa: BLE001  the cue's own row stands
        return cue.row
    data = getattr(result, "data", None) or {}
    return ((data.get("records") or {}).get("solutions") or {}).get(cue.rid) or cue.row if getattr(result, "ok", False) else cue.row


def render_questions(questions: Sequence[Mapping[str, Any]]) -> str:
    """The evidence as kept text: each question, then its answer and source, or that it is still to be decided."""
    lines = []
    for n, q in enumerate(questions, 1):
        lines.append(f"Q{n}. {q['question']}")
        lines.append(f"A{n}. {q['answer']} (source: {q['source']})" if q.get("answer") else f"A{n}. {TO_BE_DECIDED}")
    return "\n".join(lines)


QUESTION_LINE = re.compile(r"^Q(?P<n>\d+)\. (?P<q>.+)$")
ANSWER_LINE = re.compile(r"^A(?P<n>\d+)\. (?P<a>.+?)(?: \(source: (?P<s>[^)]*)\))?$")


def questions_of(text: str) -> Optional[Dict[str, Any]]:
    """The questions a kept evidence part holds (the shape ``render_questions`` writes), so the before is measured on what is kept; None when it holds none."""
    found: Dict[str, Dict[str, str]] = {}
    for line in str(text or "").splitlines():
        q, a = QUESTION_LINE.match(line.strip()), ANSWER_LINE.match(line.strip())
        if q:
            found.setdefault(q.group("n"), {})["question"] = q.group("q")
        elif a:
            answered = a.group("a").strip() != TO_BE_DECIDED and a.group("s")
            found.setdefault(a.group("n"), {}).update(answer=a.group("a").strip() if answered else "", source=(a.group("s") or "").strip() if answered else "")
    questions = [v for _, v in sorted(found.items(), key=lambda kv: int(kv[0])) if v.get("question")]
    return {"questions": questions} if questions else None


RECORD_ID = re.compile(r"\s*\((?:[a-z]{2,8}_[0-9a-f]{8,})\)|\b[a-z]{2,8}_[0-9a-f]{10,}\b")


def without_ids(text: str) -> str:
    """The text without internal record ids (``rep_4e8c0da18324``, ``esr_...``): a reader sees names, never ids."""
    return " ".join(RECORD_ID.sub("", str(text or "")).split())


def clip_line(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def milestone_pass(made: Progress, cue: Any, request: str = "") -> Dict[str, Any]:
    """The per-solution finding for the milestone's own solution, shaped from a progress step already made (it never runs the work again): what the turn
    needs (its increment), the target it moves (the intermediate metric when one was defined, else the milestone's own metric, with its total), the risk a
    person-only step leaves, and the kept rubric as knowledge. ``request`` is the person's message, kept for the reader of the finding."""
    child = made.child_metric if isinstance(made.child_metric, Mapping) else {}
    metric_id = str(child.get("id") or (cue.metric_ids[0] if getattr(cue, "metric_ids", None) else ""))
    rubric = kept_rubric(getattr(cue, "row", {}) or {})
    return {"solution_id": getattr(cue, "rid", ""), "applies": True, "request": request,
            "needs": f"{made.item}: {made.increment_line()}".strip(": ") if made.status == "measured" else (made.why or "no AI increment could be made"),
            "target": {"metric_id": metric_id, "value": made.total, "done_when": getattr(cue, "done_when", "")},
            "risks": [f"Only a person can do: {made.ask}"] if made.ask else [], "conflicts": [],
            "knowledge": [rubric_entry(rubric.item, rubric.requirements, rubric.total, rubric.source)] if rubric else [],
            "candidates": [], "before": made.before, "after": made.after}


WRITER_TIER_ENV = "QAR_MILESTONE_WRITER_TIER"


def writer_tier(cfg: Any = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """The tier a milestone deliverable is written on: ``QAR_MILESTONE_WRITER_TIER`` when set (a measured arm can be run this way), else the
    configuration's ``milestone_writer_tier``, else ``fast``. The structured calls (work items, checks, scoring) stay on the cheap tier whatever it is."""
    import os
    environ = os.environ if environ is None else environ
    return str(environ.get(WRITER_TIER_ENV) or getattr(cfg, "milestone_writer_tier", "") or "fast").strip()
