"""One measurable AI increment on a quest's milestone, for a turn whose resolved intent is progress on it ("get X moving"): the flow step that replaced the
owned-next-step write.

Role: QAR's wiring of the hub's ``CriticalThinkingComposition.progress`` for the milestones a quest keeps in Quest. A milestone is never waiting on a person:
the turn always makes something the AI can do itself and shows it measured before and after. The steps, each decided on structured fields:

1. **Work items.** One cheap call decomposes the milestone's Solution record into work items (``kind``, ``actor``, ``inputs`` as record ids, ``outward`` when
   the item publishes, sends or pays, ``moves_parent``, ``requirements``); a second, independent call says for each whether the AI can do it now with reading
   and writing only. An item is the AI's only when both agree and code finds its inputs and no outward act (the hub's ``doable_by_ai``).
   ONE AI item is then pinned by structure: what the done test is made of (things the team makes, a decision, or what other people do; a yes/no metric is a
   decision by code) fixes its kind, the decomposition only fills in its details, and once its rubric is kept it stays the item until the goal holds every
   unit it planned or the done test changes.
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
FOLLOW_SEPARATOR = "\n\n--- Then, depending on it: {item} ---\n"
FOLLOW_SPLIT = re.compile(r"\n*--- Then, depending on it: (?P<item>.+?) ---\n")
OVERVIEW_HEAD = "Overview:\n"
GROUNDING_CHARS = 12000
DEPTH_CALLS = 16       # the depth loop's call budget for one unit (work and check calls; the hub default)
RECORD_CHARS = 1500
NO_EM_DASH = "Do not use em dashes."


#: The deliverable's rubric is durable knowledge on the milestone's Solution record (its ``knowledge`` field, one entry per line, the ``[kind] claim (source: ...)``
#: shape the ESOI knowledge records migrate from), written once through ESOI Application and reused by the writer and the scorer on every later turn.
RUBRIC_KIND = "best guess"
RUBRIC_CLAIM = "Deliverable rubric for "
RUBRIC_LINE = re.compile(r"^\[" + RUBRIC_KIND + r"\] " + RUBRIC_CLAIM + r"(?P<item>[^:]+?)(?: \((?P<total>\d+) units\))?(?: \(kind (?P<kind>[a-z]+)\))?"
                         r"(?: \(done test (?P<done>[0-9a-f]{8})\))?: (?P<rubric>.+) \(source: (?P<source>[^)]*)\)\s*$")
TO_BE_DECIDED = "to be decided"


@dataclass(frozen=True)
class KeptRubric:
    """A deliverable rubric as the milestone's record keeps it."""
    item: str
    requirements: List[str]
    total: Optional[int]
    source: str
    kind: str = "deliverable"       # the work item's kind (a deliverable, or evidence)
    done: str = ""                  # the digest of the done test it was defined for; a changed done test frees the item


def done_digest(done_when: Any) -> str:
    """Eight hex characters naming a done test's words (case and spacing aside), kept with the rubric so a changed done test frees the pinned item."""
    return hashlib.sha1(squash(done_when).encode("utf-8")).hexdigest()[:8]


def kept_rubric(row: Mapping[str, Any]) -> Optional[KeptRubric]:
    """The deliverable rubric the milestone's record keeps (the last one written), else None. Reads only the line shape this module writes."""
    found = None
    for line in str(row.get("knowledge") or "").splitlines():
        match = RUBRIC_LINE.match(line.strip())
        if match:
            found = KeptRubric(match.group("item").strip(), [r.strip() for r in match.group("rubric").split(";") if r.strip()],
                               int(match.group("total")) if match.group("total") else None, match.group("source").strip(),
                               match.group("kind") or "deliverable", match.group("done") or "")
    return found


def rubric_entry(item: str, requirements: Sequence[str], total: Optional[int], source: str, kind: str = "deliverable", done: str = "") -> str:
    """One knowledge line for a rubric: the kind in brackets, the claim (item, unit count, the item's kind when not a deliverable, the done test's digest,
    and the requirements joined by "; "), and its source."""
    clean = lambda text: " ".join(str(text).replace(";", ",").replace(":", ",").replace("(", "").replace(")", "").split())      # noqa: E731
    count = f" ({total} units)" if total and total > 1 else ""
    marks = (f" (kind {kind})" if kind and kind != "deliverable" else "") + (f" (done test {done})" if done else "")
    return f"[{RUBRIC_KIND}] {RUBRIC_CLAIM}{clean(item)}{count}{marks}: " + "; ".join(clean(r) for r in requirements if str(r).strip()) + f" (source: {source})"


def with_entry(knowledge: Any, entry: str) -> str:
    """The knowledge text with ``entry`` as its rubric line: an older rubric line is replaced, never kept beside it."""
    kept = [line for line in str(knowledge or "").splitlines() if line.strip() and not RUBRIC_LINE.match(line.strip())]
    return "\n".join(kept + [entry])


class ToolSpecEdit:
    """ESOI Application at the turn's ``apply_spec_edit`` tool: the hub's upkeep writes the intermediate metric through it, under Quest's own approval rules."""

    def __init__(self, registry: Any, quest_id: str):
        self.registry, self.quest_id = registry, quest_id
        self.written: List[Dict[str, Any]] = []       # every edit this turn wrote or proposed, in order, so the reply names each kept artifact by code

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
            self.written.append({"edit": edit, "state": "proposed" if data.get("proposed") and not data.get("applied", True) else "kept"})
            return dict({"applied": True}, **data)
        if data.get("proposed"):
            self.written.append({"edit": edit, "state": "proposed"})
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
test itself is made of or published from (for published videos, their scripts; for a decision, the analysis that makes it)>,
"depends_on": "<the name of the item above whose output this one needs first, exactly as written, or empty>", "inputs": [<record ids from above it needs>],
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
"part": "<the text the requirements above are checked on, complete>"}}. List every unit of the set in "units" (as many as the set needs). The requirements are
checked on "part" alone, so "part" is the very thing they describe: when they speak of a whole set (a list, every entry, the full list), "part" is that whole
set written out, each entry with what the requirements ask of it; when they speak of one unit, "part" is the next unit in full. The part is used as it is: for a
video, the words to be spoken; for copy, the copy; for a list, the list; never notes or a plan about it.
Facts and proposals are different. A fact (what the product does, a figure, a date, a name, a result) must come from the sources above, said as they say it;
never invent one. A point about the product restates a passage of the sources in its words; an example or a speaker the sources do not give is left out. A proposal (which topics to cover, which option to recommend, how to compare candidates) is your own work: make it, concretely, from the
sources, and say why. When the work compares options or prepares a decision, end with an explicit recommendation of one option, labelled as a proposal,
with its reasoning, even when the evidence is thin (say how thin). Name an option only when a source names it, with that source; never a placeholder option
("Candidate B", "another niche"); when the sources name fewer than two, say so and name what would supply another. Write "to be decided" only for a fact that only a person or an outside system can supply,
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

ANSWER_PROMPT = """Answer one question a team's decision needs, only from the records and the reads below; quote the exact words the answer rests on.

The question: {question}

The milestone's records:
{records}

What this turn read and the research gathered (each source named):
{grounding}

Reply with JSON only: {{"answer": "<the answer, or empty when the sources do not give it>", "source": "<the record or read it comes from, by name>",
"quote": "<the exact words of that source the answer rests on, a few words>"}}. Invent no figure, name or date. {no_em_dash}"""

PART_PROMPT = """Write the few sentences one unit of a team's deliverable still lacks, as content of the unit itself (the words used as they are: for a script, the
words spoken), to be combined into it. Never write about the unit, its requirements or this instruction.

The deliverable: {item}
The unit as it stands:
{base}

What the unit still lacks: {need}

The milestone's records and this turn's reads, the only source for any fact:
{records}
{grounding}

Use only facts above; a proposal (a topic, a recommendation) is yours to make from them, with its reason. Write "to be decided" only for a fact only a
person or an outside system can supply. Reply with JSON only: {{"part": "<the sentences>"}}. {no_em_dash}"""

COMBINE_PROMPT = """Write one unit of a team's deliverable as a single coherent text, used as it is (for a script, the words spoken, in order), from the unit as it
stood and the added pieces below. Keep what each piece adds; drop repeats, notes, instructions and anything about the unit itself.

The deliverable: {item}
The unit as it stood:
{base}

Added pieces:
{pieces}

The milestone's records and this turn's reads, the only source for any fact:
{records}
{grounding}

State a fact (what the product does, a figure, a link, a name) only as the sources say it. Reply with JSON only: {{"part": "<the whole unit>"}}. {no_em_dash}"""

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
    proposed_units: List[str] = field(default_factory=list)  # the topics the writer proposed for the set's units, in order
    follow_on: Optional[Dict[str, Any]] = None                 # the next unit that depends on this one, done in the same turn: item, part, before, after, total
    method: str = ""                                           # how the increment was measured: rubric, evidence or read
    kept_artifacts: List[str] = field(default_factory=list)    # each record write this turn made or proposed besides the goal, said for the reader

    def body(self) -> str:
        """The deliverable as kept on the goal: the overview, then each part under its separator."""
        out = (OVERVIEW_HEAD + self.overview.strip()) if self.overview.strip() else ""
        for n, part in enumerate(self.parts, 1):
            out += PART_SEPARATOR.format(n=n) + part.strip()
        if self.follow_on and self.follow_on.get("part"):
            out += FOLLOW_SEPARATOR.format(item=self.follow_on["item"]) + str(self.follow_on["part"]).strip()
        return out.strip()

    def increment_line(self) -> str:
        if self.before is None or self.after is None:
            return ""
        shown = lambda v: str(int(v)) if float(v).is_integer() else f"{v:g}"     # noqa: E731
        # Every count says what it counts (its basis), so a reader can trace it to what the code counted; the depth loop's own scores stay in the record.
        basis = {"rubric": "units in which a separate check found every rubric item, each with a quote from the unit",
                 "evidence": "questions answered with a quote found in the records, the turn's reads or the research",
                 "read": "the value its source reads"}.get(self.method, "measured by code")
        of = (f" of {self.total} {'asked' if self.method == 'evidence' else 'planned'}" if self.total else "")
        line = f"{self.measure_name}: {shown(self.before)} before, {shown(self.after)} after{of} (counted: {basis}; {self.as_of})."
        if self.follow_on and self.follow_on.get("part"):
            f = self.follow_on
            line += (f" Then, depending on it, {f['item']}: {shown(f['before'])} before, {shown(f['after'])} after"
                     + (f" of {f['total']} planned" if f.get("total") else "") + " (counted the same way).")
        return line

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
        if self.kept_artifacts:
            parts.append("Also kept on the milestone's record this turn:\n" + "\n".join(f"- {a}" for a in self.kept_artifacts))
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
proposes, recommends, compares or plans, and is worded as such), or "plain" (a greeting, a call to action, a transition, or "to be decided"). A sentence that
says what a product does, has, costs or offers, or where to find it (a feature, a price, a link, an offer, a figure), is a "fact" even inside a proposal, a
spoken line or a call to action; so is an example presented as real. For a fact,
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
    # Every kept sentence, a proposal or an item's own name too, may carry a number or a status word only when the sources hold it (see ``marked_claims``).
    kept = [k for k, u in enumerate(units) if u.kept and not re.fullmatch(r"\d+[.)]", u.text.strip())]
    fixed = with_supported_markers([units[k].text for k in kept], reference, decide)
    for k, (text_now, ok) in zip(kept, fixed):
        if ok:
            units[k].text = text_now
        elif k in starts or sum(u.kept for u in units) <= 1:
            units[k].text = without_markers(units[k].text, reference)
        else:
            units[k].kept = False
            removed.append(units[k].text)
    # A sentence that leans on a removed one (a back-reference right after it) goes with it, so nothing kept rests on a fact that was taken out.
    from hubofhubs.api import cohere
    before = {k for k, u in enumerate(units) if u.kept}
    units, _ = cohere(units, "", None)
    removed += [units[k].text for k in sorted(before) if not units[k].kept]
    return joined(units), removed


#: Words that state a status (a decision made, a check done). A sentence may carry one only when the sources show it, in a sentence about the same thing.
STATUS_WORDS = re.compile(r"\b(?:agreed|confirmed|approved|verified|validated|signed off|finali[sz]ed|cross[- ]checked|checked against|double[- ]checked)\b", re.IGNORECASE)
ORDINAL_LIMIT = 10
LINK = re.compile(r"\b(?:https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|app|co|dev|me|tv|us|uk)\b(?:/\S*)?)", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
CONTENT_WORD = re.compile(r"[a-z]{5,}")
REPHRASE_PROMPT = """Each numbered sentence below states a number, a threshold or a status word ("agreed", "confirmed", "checked against ...") that the sources
do not hold. Rewrite each so it keeps its meaning without it: leave the number out, or say it is "to be set from <the named source that would give it>"; leave a
status word out, or say what is actually the case (a proposal is "proposed", not "agreed"). Never add a new number, name or date.

The sources:
{reference}

The sentences:
{sentences}

Reply with JSON only: {{"sentences": [{{"n": <number>, "text": "<the rewritten sentence>"}}]}}. {no_em_dash}"""


#: A sentence about the deliverable itself (its rubric, its requirements, what "this script" covers) is the scorer's language, never the deliverable's.
META = re.compile(r"\b(?:rubric|requirements?)\b|\bthis (?:script|unit|deliverable|draft|piece|section)\b|\b(?:the|each|every) (?:script|unit) (?:must|should|covers|names|is written)\b",
                  re.IGNORECASE)
ECHO_SHARE = 0.5        # a sentence whose content words are at least half one rubric line's (and three or more) repeats the rubric


def stems5(text: str) -> set:
    return {w[:5] for w in CONTENT_WORD.findall(str(text or "").lower())}


def without_rubric_echo(text: str, rubric: Sequence[str]) -> Tuple[str, List[str]]:
    """``(text, removed)``: the sentences that speak about the deliverable (``META``) or repeat a rubric line's words taken out by code, so the rubric and the
    scorer's wording never reach the kept text; the deliverable keeps only its own content."""
    from hubofhubs.api import joined, units_of
    units = units_of(text)
    lines = [stems5(r) for r in rubric if str(r).strip()]
    whole = [squash(r).rstrip(".") for r in rubric if len(str(r).split()) >= 2]
    removed = []
    for u in units:
        words = stems5(u.text)
        echo = (any(len(words & line) >= 3 and len(words & line) >= ECHO_SHARE * len(words) for line in lines)
                or any(w and w in squash(u.text) for w in whole))
        if (META.search(u.text) or echo) and sum(x.kept for x in units) > 1:
            u.kept = False
            removed.append(u.text)
    return joined(units), removed


def marked_claims(sentence: str, reference: str) -> List[str]:
    """The numbers and status words in ``sentence`` the sources do not hold, found by code: a number must appear in ``reference`` as a number of its own
    (thousands separators aside); a status word must appear in a sentence of ``reference`` that shares at least two content words with ``sentence``, so a
    "checked against X" claim needs a read of X that says so. [] when every one is held."""
    source = str(reference or "").replace(",", "")
    out = []
    for m in NUMBER.finditer(sentence):
        number = m.group(0).replace(",", "").rstrip(".")
        rate = re.match(r"\s*(?:%|percent\b|per cent\b)", sentence[m.end():], re.IGNORECASE)
        if number.isdigit() and int(number) <= ORDINAL_LIMIT and not rate:
            continue        # a small whole number counts or orders the deliverable's own pieces (unit 2, three points); a rate or a figure must be held
        if not re.search(r"(?<![\d.])" + re.escape(number) + r"(?![\d])", source):
            out.append(m.group(0))
    for m in LINK.finditer(sentence):
        if m.group(0).lower().rstrip("/.") not in str(reference or "").lower():
            out.append(m.group(0))
    words = set(CONTENT_WORD.findall(sentence.lower()))
    for m in STATUS_WORDS.finditer(sentence):
        status = m.group(0).lower().replace("-", " ")
        held = any(status in line.lower().replace("-", " ") and len(words & set(CONTENT_WORD.findall(line.lower())) - {status.split()[0]}) >= 2
                   for line in re.split(r"(?<=[.!?\n])\s+", str(reference or "")))
        if not held:
            out.append(m.group(0))
    return out


def with_supported_markers(sentences: Sequence[str], reference: str, decide: Optional[Callable[[str], str]]) -> List[Tuple[str, bool]]:
    """Each sentence as ``(text, ok)``: unchanged when ``marked_claims`` finds nothing; else rewritten once by ``decide`` without the unheld number or status
    word and checked again by code; ``ok`` is False when the rewrite still carries one (the caller drops the sentence, or strips the words from a name)."""
    out = [(s, not marked_claims(s, reference)) for s in sentences]
    bad = [k for k, (_, ok) in enumerate(out) if not ok]
    if not bad or decide is None:
        return out
    listing = "\n".join(f"{n}. {sentences[k]}" for n, k in enumerate(bad, 1))
    data = parse_json(decide(REPHRASE_PROMPT.format(reference=str(reference)[:GROUNDING_CHARS * 2], sentences=listing, no_em_dash=NO_EM_DASH)))
    for row in data.get("sentences") or []:
        try:
            k = bad[int(row.get("n")) - 1]
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        text = " ".join(str(row.get("text") or "").split())
        if text and not marked_claims(text, reference):
            out[k] = (text, True)
    return out


def without_markers(text: str, reference: str) -> str:
    """``text`` with the status words and numbers the sources do not hold taken out by code (for a line that must stay, such as an item's name)."""
    for marker in marked_claims(text, reference):
        text = re.sub(r"\s*\b" + re.escape(marker) + r"\b", "", text, count=1)
    return " ".join(text.split())


STEP_PROMPT = """The one step only a person can do next, after the AI's work below:
{step}

What the AI kept (the deliverable) and the records:
{kept}

Rewrite the step so everything it points to (a list, copy, a link, a script, a proposal, a scheme) is something the kept deliverable or the records hold, called
by the name it has there; drop any pointer to something they do not hold. Keep who does it when the records say. One sentence. Reply with JSON only:
{{"step": "<the step>"}}. {no_em_dash}"""
POINTER = re.compile(r"\b(?:the|its|their) (?:proposed|drafted|suggested|new|tagged) ([a-z]+(?: [a-z]+)?)", re.IGNORECASE)


def held_source(label: Any, reference: str) -> str:
    """``label`` (a source a writer named) when the turn's reads hold it: most of its content words appear in ``reference``. "" for a source the turn never
    read, so no deliverable cites one."""
    label = " ".join(str(label or "").replace("(", " ").replace(")", " ").split())
    words = stems5(label)
    if not label or not words:
        return ""
    known = stems5(reference)
    held = len(words & known)
    return label if held >= min(2, len(words)) and held / len(words) >= SOURCE_SHARE else ""


SOURCE_SHARE = 0.75      # the share of a source label's content words the reads must hold for the label to stand


def kept_step(step: str, kept: str, reference: str, decide: Optional[Callable[[str], str]]) -> str:
    """The person-only step, pointing only at what the turn kept or the records hold: a step that points at a proposed or drafted thing the kept text does not
    name (found by code) is rewritten once from the kept text and checked again; then the line rule (``checked_line``) applies."""
    step = " ".join(str(step or "").split())
    if not step:
        return step
    known = squash(kept + " " + reference)
    def missing(text: str) -> List[str]:
        out = []
        for m in POINTER.finditer(text):
            words = [w[:5] for w in re.findall(r"[a-z]{4,}", m.group(1).lower())]
            if words and not any(w in known for w in words):
                out.append(m.group(1))
        return out
    if missing(step) and decide is not None:
        again = " ".join(str(parse_json(decide(STEP_PROMPT.format(step=step, kept=(kept + "\n\n" + reference)[:GROUNDING_CHARS], no_em_dash=NO_EM_DASH))).get("step") or "").split())
        step = again if again and not missing(again) else POINTER.sub(lambda m: m.group(0) if not missing(m.group(0)) else "the kept deliverable", step)
    return checked_line(step, kept + "\n" + reference, decide)


def checked_line(text: str, reference: str, decide: Optional[Callable[[str], str]]) -> str:
    """One line the reply or the kept deliverable shows (a title, an item's name, the person-only step, a note), held to the same rule as the deliverable's
    sentences: a number or a status word only when the sources hold it."""
    text = " ".join(str(text or "").split())
    if not text:
        return text
    (now, ok), = with_supported_markers([text], reference, decide)
    return now if ok else without_markers(text, reference)


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


def kept_follow_on(text: str) -> Tuple[str, str]:
    """``(item, unit)`` of the follow-on unit a kept deliverable goal holds after its parts, else ``("", "")``."""
    pieces = FOLLOW_SPLIT.split(str(text or ""), maxsplit=1)
    return (pieces[1].strip(), pieces[2].strip()) if len(pieces) == 3 else ("", "")


def kept_parts(text: str) -> List[str]:
    """The parts a kept deliverable goal holds (after its overview), in order; [] for none."""
    body = FOLLOW_SPLIT.split(str(text or ""), maxsplit=1)[0]
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
    row = whole_row(registry, cue)          # whole records first: the work items, the writer and the checks all read past the cut
    tables = cue.tables or {}
    metrics = {mid: with_reading_fields(m) for mid, m in dict(tables.get("metrics") or {}).items()}
    rids = [cue.rid, *cue.metric_ids]
    items = work_items(cue.records_text, rids, decide)
    means = [{"name": OUTWARD, "outward": True}]
    doable = lambda item: doable_by_ai(item, means, rids)      # noqa: E731
    spec_edit = ToolSpecEdit(registry, cue.quest_id)
    rubric = kept_rubric(row)
    rubric_state = ""
    knowledge_now = str(row.get("knowledge") or "")
    knowledge_start = knowledge_now          # the record's knowledge before this turn wrote anything (the kept-artifact list names what is new)
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
    # The milestone's keyed goal is the one Quest AI already keeps for it (linked to the milestone's record), updated in place; only a milestone with none gets a new one.
    kept_goal = next((g for g in cue.candidates if str(g.get("spec_solution_id") or "") == cue.rid and is_owned(g)), None)
    kept_text = str((kept_goal or {}).get("text") or (kept_goal or {}).get("description") or "")
    digest = done_digest(cue.done_when)
    # The pinned item stays until its rubric is met (the goal holds every unit it planned) or the done test changes.
    if rubric is not None and ((rubric.done and rubric.done != digest) or (rubric.total and len(kept_parts(kept_text)) >= rubric.total)):
        rubric = None
    metric_name = str((metrics.get(cue.metric_ids[0]) or {}).get("name") or "") if cue.metric_ids else ""
    reference_now = "\n".join(t for t in (cue.records_text, cue.allowed_text(), str(grounding or ""), day) if t)
    out_home: Dict[str, Any] = {}       # the deliverable's own solution (classified, or created when none fits)
    home_rubric = None
    if rubric is not None:
        # The record keeps the work item and its rubric from an earlier turn: that is the work item now, for the writer and the scorer alike. It was agreed when it
        # was defined (both checks), so it is not judged again; the work items of this turn only give the person-only step and what moves the metric.
        others = [i for i in items if not doable(i)]
        items = [{"name": rubric.item, "kind": rubric.kind, "actor": "ai", "outward": False, "moves_parent": False, "inputs": [cue.rid], "tools": [],
                  "checks": [True, True], "total": rubric.total or 1, "requirements": list(rubric.requirements), "kept_rubric": True}] + others
        rubric_state = "reused"
    else:
        # The work item is pinned by structure (what the done test is made of, and what the metric's source allows): the decomposition fills in its details only.
        kind_of_test = done_test_class(cue, cue.current_text() if cue.reading else cue.value, decide)
        items = pinned_items(items, kind_of_test, doable, cue, metric_name, cue.records_text, decide)
        first = next((i for i in items if str(i.get("kind") or "") in ("deliverable", "evidence") and doable(i) and i.get("requirements")), None)
        if first is not None:
            # The item's own name is held to the sentence rule (no status word or number the sources do not hold) before it is kept anywhere.
            first["name"] = checked_line(first["name"], reference_now, decide) or first["name"]
            # Part 1 of work quality: the deliverable is classified to its own solution among the quest's records (the hub's placement path), and that
            # solution is created when none fits. Its record owns the deliverable's rubric, so any milestone making this kind of thing reuses it.
            home = deliverable_home(cue, first, spec_edit, decide)
            home_row = dict((home or {}).get("row") or {})
            home_rubric = kept_rubric(home_row) if home_row else None
            out_home.update(home or {})
        if first is not None and home_rubric is not None:
            first.update(name=home_rubric.item, kind=home_rubric.kind, requirements=list(home_rubric.requirements), kept_rubric=True)
            if home_rubric.total:
                first["total"] = home_rubric.total
            rubric_state = "reused from its solution"
        elif first is not None:
            # Defined once, with depth: what a good deliverable of this kind is, researched from the product's documents, cited sources and the done test,
            # agreed by a second view, and kept on the deliverable's own solution record (else the milestone's) through ESOI Application (the same gate and
            # autopilot rules as any spec edit), so every later turn reuses it instead of drifting.
            researched, cited = researched_rubric(first, cue, research, decide) if first["kind"] == "deliverable" else ([], [])
            if researched:
                pinned = [r for r in first["requirements"] if r == DECISION_REQUIREMENT]
                first["requirements"] = pinned + [r for r in researched if r not in pinned][:5 - len(pinned)]
            owner = str(out_home.get("id") or "")
            entry = rubric_entry(first["name"], first["requirements"], first.get("total") if isinstance(first.get("total"), int) else None,
                                 ", ".join(cited + [f"{cue.rid}", "Quest AI", day]), kind=first["kind"], done="" if owner else digest)
            stored = kept_rubric({"knowledge": entry})
            first["requirements"] = stored.requirements if stored else first["requirements"]     # the words as stored, so a later turn's reuse scores the same rubric
            held = home_row.get("knowledge") if owner else row.get("knowledge")
            answer = spec_edit.apply({"edit": {"kind": "spec_edit", "target": owner or cue.rid, "field": "knowledge", "value": with_entry(held, entry)}})
            if answer.get("applied") and not owner:
                knowledge_now = with_entry(row.get("knowledge"), entry)
            rubric_state = "kept" if answer.get("applied") else ("proposed" if answer.get("proposed") else "not kept: " + str(answer.get("reason") or "refused")[:200])
    moved_by = "person" if [i for i in items if i.get("moves_parent")] and not any(doable(i) for i in items if i.get("moves_parent")) else "ai"
    title = str((kept_goal or {}).get("title") or (kept_goal or {}).get("name") or f"{cue.name} {DELIVERABLES}").strip()
    # What the goal already holds, measured again for the before: its parts for a rubric, its answered questions for evidence (nothing kept scores 0).
    kept = dict({"parts": kept_parts(kept_text)}, **(questions_of(kept_text) or {})) if kept_text else None
    milestone = {"solution_id": cue.rid, "name": cue.name, "done_when": cue.done_when, "moved_by": moved_by, "records": rids,
                 "lands_on": {"collection": "goals", "title_prefix": cue.name}, "knowledge": knowledge_now, "today": day,
                 "sources": [cue.source] if cue.source else []}
    decision = MajorityVote(plumbing=None)
    composition = CriticalThinkingComposition(None, spec_edit, None, None, decision, RecordFieldUpkeep(esoi=spec_edit, decision=decision))
    out = Progress(title=title, metric_name=metric_name,
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
            if DECISION_REQUIREMENT in (item.get("requirements") or []):
                # A decision compares options: the options themselves are researched too (the product's documents and outside sources that name them).
                options = str(research(f"options to choose between for: {cue.done_when}. {cue.purpose}"[:600]) or "").strip()
                found["text"] = "\n\n".join(t for t in (found["text"], options) if t)
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

    cleaned_cache: Dict[str, str] = {}
    rubric_lines: List[str] = [str(r) for i in items for r in i.get("requirements") or []]     # the scorer's words, never the deliverable's

    def cleaned(text: str) -> str:
        """``text`` as it will be kept: sentences about the deliverable or repeating its rubric out, each claim checked by ``grounded_text``, then the sentence
        check (facts on a source quote, numbers, links and status words held, and what leans on a removed sentence removed with it). Once per text."""
        text = str(text or "").strip()
        if not text:
            return ""
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in cleaned_cache:
            return cleaned_cache[key]
        now, echoed = without_rubric_echo(text, rubric_lines)
        out.unsupported += echoed
        allowed = sources_text()
        now = grounded_text(now, allowed, decide) or ""
        if now:
            now, dropped = supported_sentences(now, allowed, decide)
            out.unsupported += dropped
        cleaned_cache[key] = now.strip()
        return cleaned_cache[key]

    def write_unit(item: Mapping[str, Any], requirements: List[str], have: List[str], note: str = "") -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
        """One unit of a deliverable: written on the writer's tier from the records, the reads and the research, then built out by the hub's depth loop when the
        separate check finds it short of its requirements. Returns the unit, the writer's answer (its proposed units) and the depth account."""
        total = item.get("total") if isinstance(item.get("total"), int) else None
        kept_note = (f"Already kept (do not repeat; write the next unit, number {len(have) + 1}):\n" + "\n".join(f"- {clip_line(p)}" for p in have)) if have else ""
        kept_note = (kept_note + "\n" + note).strip()
        data = parse_json(write(WRITE_PROMPT.format(records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(),
                                                    item=item.get("name"), requirements="; ".join(requirements) or "(none given)",
                                                    kept=kept_note, total=total or "the", no_em_dash=NO_EM_DASH)))
        part = str(data.get("part") or "").strip()
        depth = None
        if part and requirements:
            # The unit is its own solution with the kept rubric as its metric: the hub's depth loop checks it (the separate scorer, never the writer), splits what
            # it misses into sub-pieces with their own sub-prompts, recombines and checks again within the request's budget.
            from hubofhubs.api import build_out
            prompt = WRITE_PROMPT.format(records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(),
                                         item=item.get("name"), requirements="; ".join(requirements), kept=kept_note, total=total or "the", no_em_dash=NO_EM_DASH)
            unit = {"goal": f"{item.get('name')}, unit {len(have) + 1}", "requirements": requirements, "prompt": prompt, "solution_id": cue.rid, "whole": True}

            def piece_work(piece: Mapping[str, Any]) -> str:
                answer = str(write(str(piece.get("prompt") or "")) or "")
                # The part as asked (JSON); a plain-text answer is the part itself; an empty or malformed JSON answer is no part (never raw JSON in the unit).
                return str(parse_json(answer).get("part") or ("" if answer.strip().startswith("{") else answer)).strip()

            def piece_check(text: str, piece: Mapping[str, Any]) -> Dict[str, Any]:
                # The whole unit is scored as it will be kept (after the sentence check), so depth stops on the scorer's verdict of the kept words; a sub-piece
                # is a draft of sentences for the combine pass and is scored as written.
                text = cleaned(text) if piece.get("whole") else text
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
                         "prompt": PART_PROMPT.format(item=item.get("name"), base=part, need=req, records=cue.records_text[:RECORD_CHARS * 2],
                                                      grounding=shown(), no_em_dash=NO_EM_DASH)} for req in missing]

            def piece_combine(piece: Mapping[str, Any], base: str, parts: Sequence[Mapping[str, Any]]) -> str:
                # One pass writes the whole unit again from the unit as it stood, the pieces and the sources, as a single text (stitched pieces read as notes);
                # with no answer the pieces are joined as written. The loop then scores it as kept and keeps the better of before and after.
                pieces = [str(p["text"]).strip() for p in parts if str(p.get("text") or "").strip()]
                if not pieces:
                    return base
                answer = parse_json(write(COMBINE_PROMPT.format(item=item.get("name"), base=base.strip(), pieces="\n\n".join(f"- {x}" for x in pieces),
                                                                records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(), no_em_dash=NO_EM_DASH)))
                return str(answer.get("part") or "").strip() or "\n\n".join([base.strip()] + pieces)
            deep = build_out(unit, piece_work, piece_check, split=piece_split, combine=piece_combine, budget={"calls": DEPTH_CALLS, "depth": 2, "width": 4}, base=part)
            depth = {k: deep.get(k) for k in ("before", "after", "calls", "levels", "stopped", "met")}
            if float(deep.get("after") or 0) >= float(deep.get("before") or 0):
                part = str(deep.get("text") or part).strip()       # depth that scored lower than the one-call unit is not kept
        # The sentence check runs once on the final text (the same cached pass the loop scored), so the kept words are the ones the scorer judged.
        return cleaned(part), data, depth

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
        part, data, depth = write_unit(item, [str(r) for r in increment.get("requirements") or []], have)
        out.depth = depth
        allowed = sources_text()
        # The list of units is a proposal: the writer proposes what each covers, with its reason and source; its facts go through the same claim check.
        units = [u for u in data.get("units") or [] if isinstance(u, Mapping) and str(u.get("topic") or "").strip()]
        out.proposed_units = [" ".join(str(u["topic"]).split()) for u in units]
        proposed = "\n".join(f"{n}. {' '.join(str(u['topic']).split())}. Why: {' '.join(str(u.get('why') or '').split()) or 'see the sources'}"
                             + (f" (source: {held_source(u.get('source'), allowed)})" if held_source(u.get("source"), allowed) else "") for n, u in enumerate(units, 1))
        head = checked_line(f"{item.get('name')}" + (f": {total} units, written one per turn, in order." if total and total > 1 else "."), allowed, decide)
        if proposed:
            # A proposed topic is a proposal and stays; the facts given as its reasons must rest on a source like any other.
            proposed, dropped = supported_sentences(grounded_text(proposed, allowed, decide), allowed, decide, keep_line_starts=True)
            out.unsupported += dropped
        # When the part is itself the set (most proposed topics are in it), the list is not repeated above it.
        topics = [stems5(t) for t in out.proposed_units if stems5(t)]
        in_part = sum(1 for t in topics if len(t & stems5(part)) * 2 >= len(t))
        if topics and in_part * 2 >= len(topics):
            proposed = ""
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
            grounded = (bool(str(row.get("answer") or "").strip()) and bool(held_source(row.get("source"), sources_text())) and len(quote) >= 3 and quote in text)
            questions.append({"question": " ".join(str(row["question"]).split()), "answer": " ".join(str(row.get("answer") or "").split()) if grounded else "",
                              "source": " ".join(str(row.get("source") or "").split()) if grounded else ""})
        questions = questions[:EVIDENCE_QUESTIONS]
        out.depth = deepen_evidence(questions, item)
        rendered = render_questions(questions) if questions else ""
        if rendered:
            # The answers go through the same sentence check as any deliverable: each fact in them must rest on words code finds in the sources. An answer the
            # check emptied is open again ("to be decided", no source), and the evidence is rendered again from the questions, so the counts match the text.
            checked, dropped = supported_sentences(rendered, sources_text(), decide, keep_line_starts=True)
            out.unsupported += dropped
            questions = (questions_of(checked) or {}).get("questions") or questions
            rendered = render_questions(questions)
        out.parts = [rendered] if rendered else []
        out.overview = f"The questions the decision needs, each answered from a cited record or read; the rest are {TO_BE_DECIDED}."
        return {"artifact": {"questions": questions} if questions else None, "evidence_ref": title}

    def deepen_evidence(questions: List[Dict[str, Any]], item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Depth for evidence: each question the first pass left open is its own piece, with its own research (the question itself, against the product's
        documents and outside sources) and its own answering call; an answer stands only with a quote code finds in the sources and a source the turn read.
        Pieces run until every question is answered or the budget is spent. Returns the depth account (answered share before and after, calls, why it stopped)."""
        if not questions:
            return None
        share = lambda: sum(1 for q in questions if q.get("answer")) / len(questions)      # noqa: E731
        before, calls = share(), 0
        for q in [q for q in questions if not q.get("answer")]:
            if calls + 2 > DEPTH_CALLS:
                break
            more = ""
            if research is not None:
                calls += 1
                try:
                    more = str(research(f"{q['question']} {cue.name}"[:600]) or "").strip()
                except Exception as error:  # noqa: BLE001  the question stays open
                    log.warning("milestone progress: question research failed: %s: %s", type(error).__name__, error)
                if more:
                    found["text"] = "\n\n".join(t for t in (found.get("text", ""), more) if t)
                    out.sources = found["text"]
            calls += 1
            data = parse_json(write(ANSWER_PROMPT.format(question=q["question"], records=cue.records_text[:RECORD_CHARS * 2], grounding=shown(), no_em_dash=NO_EM_DASH)))
            quote, allowed = squash(data.get("quote")), sources_text()
            if str(data.get("answer") or "").strip() and len(quote) >= 3 and quote in squash(allowed) and held_source(data.get("source"), allowed):
                q.update(answer=" ".join(str(data["answer"]).split()), source=" ".join(str(data["source"]).split()))
        after = share()
        return {"before": round(before, 3), "after": round(after, 3), "calls": calls, "levels": 1 if calls else 0,
                "stopped": "met" if after == 1 else ("budget" if calls + 2 > DEPTH_CALLS else "rechecked"), "met": after == 1}

    made = composition.progress(milestone, metrics, items, work, judge=judge, means=means, readers=readers, today=day, ask=decide,
                                kept=kept)
    def follow_on_unit(follow: Dict[str, Any]) -> Dict[str, Any]:
        researched, _ = researched_rubric(follow, cue, research, decide)
        requirements = researched or [str(r) for r in follow.get("requirements") or []]
        first = out.proposed_units[0] if out.proposed_units else ""
        kept_item, kept_unit = kept_follow_on(kept_text)
        before = 1.0 if kept_unit and requirements and all(judge(r, kept_unit) for r in requirements) else 0.0
        part, _, depth = write_unit(follow, requirements, [], note=(f"Write the unit for the first proposed item: {first}." if first else ""))
        met = bool(part) and bool(requirements) and all(judge(r, part) for r in requirements)
        total = follow.get("total") if isinstance(follow.get("total"), int) and follow.get("total") > 1 else None
        return {"item": str(follow.get("name") or ""), "part": part, "before": before, "after": max(before, 1.0 if met else 0.0), "total": total,
                "requirements": requirements, "depth": depth}

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
    out.ask = kept_step(without_ids(first_person_step(items, doable)), out.body(), sources_text(), decide)
    out.method = str(increment.get("method") or "")
    if out.status == "measured" and increment.get("kind") == "deliverable":
        # The ladder follows dependencies inside the turn: the next AI-doable unit that needs this one (a script for the first item of a proposed list) is
        # done now too, measured the same way, so a proposal never stops the turn at waiting for it to be agreed.
        follow = dependent_item(items, str(item.get("name") or ""), doable)
        if follow is not None:
            out.follow_on = follow_on_unit(follow)
    # Every artifact this turn kept or proposed on the records is named in the reply, from the writes code recorded (never from what the writer says).
    out.kept_artifacts = kept_artifacts(spec_edit.written, knowledge_start, metrics)
    if out.status != "measured":
        out.why = str(made.get("message") or out.status)
        return out
    result = registry.invoke(KEEP_TOOL, {"quest_id": cue.quest_id, "title": title, "body": out.body(), "solution_id": cue.rid,
                                         "increment": {"kind": increment.get("kind"), "measure_id": increment.get("measure_id"), "measure_name": out.measure_name,
                                                       "before": out.before, "after": out.after, "total": out.total, "method": increment.get("method"),
                                                       "as_of": day, "changed": out.after != out.before, "depth": out.depth,
                                                       "follow_on": {k: (out.follow_on or {}).get(k) for k in ("item", "before", "after", "total")} if out.follow_on else None}},
                         ToolContext(quest_id=cue.quest_id))
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
    fixed = [str(r) for r in item.get("requirements") or [] if str(r) == DECISION_REQUIREMENT]
    data = parse_json(decide(RUBRIC_PROMPT.format(item=item.get("name"), done_when=cue.done_when, purpose=clip_line(cue.purpose, 600), research=found[:GROUNDING_CHARS])
                             + ("\nEvery requirement must agree with this fixed one (never ask for the opposite, such as a single option where it compares several): "
                                + fixed[0] if fixed else "")))
    proposed = [r for r in data.get("requirements") or [] if isinstance(r, Mapping) and str(r.get("text") or "").strip()][:5]
    if not proposed:
        return [], []
    listing = "\n".join(f"{n}. {r['text']}" for n, r in enumerate(proposed, 1))
    verdicts = {int(v.get("n")): v.get("keep") is True for v in parse_json(decide(RUBRIC_CHECK_PROMPT.format(
        item=item.get("name"), research=found[:GROUNDING_CHARS], listing=listing, done_when=cue.done_when)
        + ("\nKeep a requirement only when it also agrees with this fixed one: " + fixed[0] if fixed else ""))).get("verdicts") or []
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


#: What a milestone's done test is made of, which fixes the kind of work the AI does for it (the cheap writer only fills in the details within that item).
MADE, DECISION, OTHERS = "made", "decision", "others"
YES_NO = {"yes", "no", "true", "false"}
CLASS_PICK = "What is this milestone's done test made of? Pick one."
DECISION_REQUIREMENT = ("Names each option a record or a source names, each with that source (never a placeholder option), says plainly when the sources "
                        "give fewer than two, compares them on the evidence that exists, and recommends one, labelled as a proposal, with its reasoning")
EVIDENCE_REQUIREMENTS = ["Each answer names its source and rests on words in it", "Says plainly what the sources do not give"]


def done_test_class(cue: Any, value: Any, decide: Callable[[str], str]) -> str:
    """``made`` (things the team makes and publishes: the AI writes the next one), ``decision`` (a choice: the AI writes the comparison and the recommended
    option) or ``others`` (what other people do: the AI gathers the evidence and, through upkeep, proposes how the metric becomes readable). A yes/no metric is a
    decision by code; otherwise Decision Check's two views pick, the draft ``made`` standing when they abstain."""
    if str(value or "").strip().split(" ")[0].strip(".,:;").lower() in YES_NO:
        return DECISION
    from hubofhubs.api import MajorityVote
    options = [{"id": MADE, "text": "things the team itself makes and publishes or sends (videos, posts, copy, documents): their content can be written now"},
               {"id": DECISION, "text": "a choice the team makes between options (which niche, which plan): its analysis can be written now"},
               {"id": OTHERS, "text": "what other people do (sign-ups, purchases, replies, users from a source): it is counted, not written"}]
    evidence = f"Milestone: {cue.name}. Done when: {cue.done_when}. What it is for: {clip_line(cue.purpose, 400)}. Its metric now: {clip_line(value, 200)}."
    binding = {"kind": "pick", "options": options, "views": [{"id": "done-test", "instruction": CLASS_PICK, "evidence": evidence},
                                                              {"id": "counted", "instruction": CLASS_PICK + " Read what the metric counts first.", "evidence": evidence}],
               "max_calls": 3, "parallel": 2}
    try:
        got = MajorityVote(plumbing=None).check(MADE, binding, decide)
    except Exception:  # noqa: BLE001  the draft stands
        return MADE
    return str(got.get("pick") or MADE) if str(got.get("pick") or "") in (MADE, DECISION, OTHERS) else MADE


def pinned_items(items: List[Dict[str, Any]], kind_of_test: str, doable: Callable[[Mapping[str, Any]], bool], cue: Any, metric_name: str,
                 records: str, decide: Callable[[str], str]) -> List[Dict[str, Any]]:
    """The work items with ONE AI item pinned by structure first: for ``made`` the deliverable the done test is made from (confirmed by Decision Check
    over the decomposition's done-material deliverables), for ``decision`` the comparison and recommendation, for ``others`` the evidence. The decomposition
    only fills in the pinned item's name and details; when it has no item of that kind, code builds it. Every other AI item is dropped, except the ones that
    depend on the pinned item (done after it in the same turn); the person's items stay for the person-only step."""
    ai = [i for i in items if doable(i)]
    if kind_of_test == OTHERS:
        pin = next((dict(i) for i in ai if str(i.get("kind") or "") == "evidence"), None) or {
            "name": f"Evidence on {metric_name or cue.name} from the records and this turn's reads", "actor": "ai", "outward": False, "moves_parent": False,
            "inputs": [cue.rid], "tools": [], "checks": [True, True], "total": 1, "requirements": list(EVIDENCE_REQUIREMENTS)}
        pin.update(kind="evidence", requirements=[str(r) for r in pin.get("requirements") or []] or list(EVIDENCE_REQUIREMENTS))
    elif kind_of_test == DECISION:
        pin = next((dict(i) for i in ai if str(i.get("kind") or "") in ("recommendation", "deliverable") and i.get("done_material") is True), None) or next(
            (dict(i) for i in ai if str(i.get("kind") or "") == "recommendation"), None) or {
            "name": f"Comparison of the options for {cue.name}, with a recommended one", "actor": "ai", "outward": False, "moves_parent": False,
            "inputs": [cue.rid], "tools": [], "checks": [True, True], "total": 1, "requirements": []}
        pin.update(kind="deliverable", requirements=[DECISION_REQUIREMENT] + [str(r) for r in pin.get("requirements") or [] if str(r) != DECISION_REQUIREMENT][:3])
    else:
        made = [i for i in ai if str(i.get("kind") or "") == "deliverable" and i.get("done_material") is True] or [
            i for i in ai if str(i.get("kind") or "") == "deliverable"]
        if not made:
            return items
        pin = dict(confirmed_item_order(made, doable, records, decide)[0], kind="deliverable")
    key = " ".join(str(pin.get("name") or "").lower().split())
    after = [] if pin["kind"] == "evidence" else [i for i in ai if " ".join(str(i.get("depends_on") or "").lower().split()) == key and str(i.get("kind") or "") == "deliverable"
             and " ".join(str(i.get("name") or "").lower().split()) != key]
    return [pin] + after + [i for i in items if not doable(i)]


KNOWLEDGE_KIND = re.compile(r"^\[[^\]]+\]\s*")
KNOWLEDGE_SOURCE = re.compile(r"\s*\(source: [^)]*\)\s*$")
SOURCE_PROPOSAL = re.compile(r"^Source proposal for metric (?P<mid>\S+): (?P<rest>.+)$")


def kept_artifacts(written: Sequence[Mapping[str, Any]], knowledge_before: str, metrics: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """One line per artifact the turn's record writes kept or proposed (in order): a knowledge line added to the milestone's record (the deliverable rubric,
    a source proposal), a metric added, or a field changed, each said in words a reader follows, without record ids."""
    before = {line.strip() for line in str(knowledge_before or "").splitlines() if line.strip()}
    out: List[str] = []
    for entry in written:
        edit = dict(entry.get("edit") or {})
        held = " (proposed, waiting for approval)" if entry.get("state") == "proposed" else ""
        if edit.get("field") == "knowledge":
            for line in str(edit.get("value") or "").splitlines():
                line = line.strip()
                if not line or line in before:
                    continue
                before.add(line)
                rubric = kept_rubric({"knowledge": line})
                if rubric is not None:
                    out.append(f"The rubric each unit of {rubric.item} is scored against: " + "; ".join(rubric.requirements) + held)
                    continue
                claim = KNOWLEDGE_SOURCE.sub("", KNOWLEDGE_KIND.sub("", line))
                proposal = SOURCE_PROPOSAL.match(claim)
                if proposal:
                    name = str((metrics.get(proposal.group("mid")) or {}).get("name") or "the milestone metric")
                    out.append(f"A proposal (the AI's, to be agreed) for making {name} readable here: {proposal.group('rest')}" + held)
                else:
                    out.append(without_ids(claim) + held)
        elif edit.get("kind") == "record_add":
            out.append(f"A new {str(edit.get('table') or 'record').rstrip('s')} record: {without_ids(str((edit.get('fields') or {}).get('name') or edit.get('key') or ''))}" + held)
        elif edit.get("field"):
            out.append(f"The {edit.get('field')} field of a record, updated" + held)
    return [" ".join(a.split()).rstrip(".") + "." for a in out if a.strip()]


HOME_PICK = ("Which of these solutions is the one whose own work makes this deliverable (its records describe making this kind of thing)? Pick none when "
             "none of them is.")
KIND_PROMPT = """A team's work item: {item} (for the milestone: {milestone}).

Name the KIND of deliverable it is, generically, so other work of the same kind is filed with it: a plural noun phrase of 2 to 4 words (for example "video
scripts", "option comparisons", "funnel evidence answers"). Reply with JSON only: {{"kind": "<the name>", "purpose": "<one sentence: what this kind of deliverable
is for>"}}. Do not use em dashes."""
NONE_FITS = "none"


def deliverable_home(cue: Any, item: Mapping[str, Any], spec_edit: Any, decide: Callable[[str], str]) -> Optional[Dict[str, Any]]:
    """The deliverable's own solution: the quest's records classified by the hub's placement path (the records index the solution consideration uses), the pick confirmed by Decision Check's two views among the found solutions and "none"; when none fits, a solution for this kind of deliverable is
    created through ESOI Application. ``{"id", "row", "created"}``, or None when nothing could be classified or created."""
    from hubofhubs.api import MajorityVote
    from hubofhubs.api import RecordsIndex
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.solution_consideration import quest_network
    tables = cue.tables or {}
    solutions = dict(tables.get("solutions") or {})
    try:
        # The records index ranks the quest's solutions for the deliverable (no model call); the gate the consideration passes use is for scope, not filing,
        # so the best few are offered and Decision Check's views decide.
        found = [f for f in RecordsIndex().relevant(f"{item.get('name')}. {cue.name}", quest_network(str(cue.quest_id), tables), depth=1)["solutions"]
                 if str(f.get("solution_id") or "") not in ("", cue.rid) and str(f.get("solution_id")) in solutions][:4]
    except Exception as error:  # noqa: BLE001  no classification: the solution is created
        log.warning("milestone progress: classifying the deliverable failed: %s: %s", type(error).__name__, error)
        found = []
    if found:
        options = [{"id": str(f["solution_id"]), "text": f"{solutions[str(f['solution_id'])].get('name') or f.get('name')}: "
                    + clip_line(solutions[str(f['solution_id'])].get('purpose'), 200)} for f in found] + [
            {"id": NONE_FITS, "text": "none of these makes this kind of deliverable"}]
        evidence = f"The deliverable: {item.get('name')} (for the milestone {cue.name}).\nThe solutions:\n" + "\n".join(f"{o['id']}: {o['text']}" for o in options)
        binding = {"kind": "pick", "options": options, "views": [{"id": "makes-it", "instruction": HOME_PICK, "evidence": evidence},
                                                                  {"id": "kind-of-thing", "instruction": HOME_PICK + " Read the deliverable first.", "evidence": evidence}],
                   "max_calls": 3, "parallel": 2}
        try:
            pick = str(MajorityVote(plumbing=None).check(NONE_FITS, binding, decide).get("pick") or NONE_FITS)
        except Exception:  # noqa: BLE001  the draft (none) stands
            pick = NONE_FITS
        if pick in solutions:
            return {"id": pick, "row": dict(solutions[pick]), "created": False}
    data = parse_json(decide(KIND_PROMPT.format(item=item.get("name"), milestone=cue.name)))
    kind = " ".join(str(data.get("kind") or "").split()).strip(" .")
    if not kind:
        return None
    purpose = " ".join(str(data.get("purpose") or "").split()) or f"Makes {kind}."
    fields = {"name": kind[:1].upper() + kind[1:], "purpose": purpose}
    answer = spec_edit.apply({"edit": {"kind": "record_add", "table": "solutions", "key": fields["name"], "fields": fields}})
    sid = str(answer.get("target") or "")
    if not answer.get("applied") or not sid:
        return None
    return {"id": sid, "row": fields, "created": bool(answer.get("created", True))}


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
    cue holds when the lane cannot read it whole. The same read returns the milestone's metric records whole, and the cue's ``records_text`` (what the writer,
    the claim check and the reader of the turn's reads see) is rebuilt from them, so a fact past the cut is a fact the turn read."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    if registry is None or registry.get("read_spec_records") is None:
        return cue.row
    try:
        result = registry.invoke("read_spec_records", {"quest_id": cue.quest_id, "whole_record_ids": [cue.rid, *cue.metric_ids]}, ToolContext(quest_id=cue.quest_id))
    except Exception:  # noqa: BLE001  the cue's own row stands
        return cue.row
    data = getattr(result, "data", None) or {}
    if not getattr(result, "ok", False):
        return cue.row
    records = data.get("records") or {}
    row = (records.get("solutions") or {}).get(cue.rid) or cue.row
    if records.get("metrics") and isinstance(cue.tables, Mapping):
        tables = {k: dict(v) for k, v in cue.tables.items()}
        tables["metrics"] = dict(tables.get("metrics") or {}, **{mid: m for mid, m in (records.get("metrics") or {}).items() if mid in cue.metric_ids})
        tables["solutions"] = dict(tables.get("solutions") or {}, **{cue.rid: row})
        cue.tables = tables
        cue.records_text = milestone_cue.records_read(tables, cue.rid, row)
    return row


def render_questions(questions: Sequence[Mapping[str, Any]]) -> str:
    """The evidence as kept text: each question, then its answer and source, or that it is still to be decided."""
    lines = []
    for n, q in enumerate(questions, 1):
        lines.append(f"Q{n}. {q['question']}")
        lines.append(f"A{n}. {q['answer']} (source: {q['source']})" if q.get("answer") else f"A{n}. {TO_BE_DECIDED}")
    return "\n".join(lines)


SOURCE_LABEL = re.compile(r"\(source: [^)]*\)")
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
            answer = SOURCE_LABEL.sub("", a.group("a")).strip(" .;:,")
            answered = answer and answer != TO_BE_DECIDED and a.group("s")
            found.setdefault(a.group("n"), {}).update(answer=a.group("a").strip() if answered else "", source=(a.group("s") or "").strip() if answered else "")
    questions = [v for _, v in sorted(found.items(), key=lambda kv: int(kv[0])) if v.get("question")]
    return {"questions": questions} if questions else None


def dependent_item(items: Sequence[Dict[str, Any]], name: str, doable: Callable[[Mapping[str, Any]], bool]) -> Optional[Dict[str, Any]]:
    """The AI-doable deliverable whose ``depends_on`` names ``name`` (case and spacing aside), else None."""
    key = " ".join(name.lower().split())
    return next((i for i in items if str(i.get("kind") or "") == "deliverable" and doable(i) and " ".join(str(i.get("name") or "").lower().split()) != key
                 and key and " ".join(str(i.get("depends_on") or "").lower().split()) == key), None)


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
            "knowledge": [rubric_entry(rubric.item, rubric.requirements, rubric.total, rubric.source, rubric.kind, rubric.done)] if rubric else [],
            "candidates": [], "before": made.before, "after": made.after}


WRITER_TIER_ENV = "QAR_MILESTONE_WRITER_TIER"


def writer_tier(cfg: Any = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """The tier a milestone deliverable is written on: ``QAR_MILESTONE_WRITER_TIER`` when set (a measured arm can be run this way), else the
    configuration's ``milestone_writer_tier``, else ``fast``. The structured calls (work items, checks, scoring) stay on the cheap tier whatever it is."""
    import os
    environ = os.environ if environ is None else environ
    return str(environ.get(WRITER_TIER_ENV) or getattr(cfg, "milestone_writer_tier", "") or "fast").strip()
