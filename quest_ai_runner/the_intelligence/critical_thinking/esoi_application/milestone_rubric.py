"""The deliverable rubric and how a unit is scored against it: what a good unit must meet, the kept rubric line, the research-backed definition, the done-test class and the deliverable's own solution.

Role: defines the standard before the work is written and holds the judge that is not the writer. The rubric is durable knowledge on the Solution record; ``Scorer`` counts a requirement only with a quote found in the text; ``done_test_class`` and ``pinned_items`` fix what kind of item the milestone needs; ``deliverable_home`` files the deliverable under its own solution.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .milestone_facts import GROUNDING_CHARS, RECORD_CHARS, clip_line, log, parse_json, squash, without_ids


#: The deliverable's rubric is durable knowledge on the milestone's Solution record (its ``knowledge`` field, one entry per line, the ``[kind] claim (source: ...)``
#: shape the ESOI knowledge records migrate from), written once through ESOI Application and reused by the writer and the scorer on every later turn.
RUBRIC_KIND = "best guess"


RUBRIC_CLAIM = "Deliverable rubric for "


RUBRIC_LINE = re.compile(r"^\[" + RUBRIC_KIND + r"\] " + RUBRIC_CLAIM + r"(?P<item>[^:]+?)(?: \((?P<total>\d+) units\))?(?: \(kind (?P<kind>[a-z]+)\))?"
                         r"(?: \(done test (?P<done>[0-9a-f]{8})\))?: (?P<rubric>.+) \(source: (?P<source>[^)]*)\)\s*$")


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


SCORE_PROMPT = """Check one deliverable against its requirements. You did not write it; judge only what the text shows, against the records below.

The records and reads it may rest on (for a requirement about what the records say):
{reference}

The text:
{part}

Requirements:
{requirements}

For each requirement, say whether the text meets it, and quote the words of the text that show it (an exact copy of a few words). Reply with JSON only:
{{"verdicts": [{{"n": <number>, "meets": true | false, "quote": "<exact words from the text, empty when it does not meet it>"}}]}}."""


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
                    out.append(f"The rubric for {rubric.item} ({len(rubric.requirements)} requirements each unit is checked on), kept on its solution's record" + held)
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
{records}
Name the KIND of deliverable it is, generically, so other work of the same kind is filed with it, and frame the solution that makes this kind of thing. Use only the
work item, the milestone and the records above; state nothing they do not support, and leave a field out (empty string) rather than guess it. Reply with JSON only:
{{"name": "<the kind: a plural noun phrase of 2 to 4 words, for example video scripts>", "purpose": "<one sentence: what this kind of deliverable is for>",
"approach": "<one sentence starting with a verb, naming no actor: how this kind of deliverable is made>",
"problem": {{"what": "<the problem this kind of deliverable solves>", "who": "<who has it>", "why": "<why it matters>"}},
"ideal": {{"name": "<the state when the problem is solved, as a short name>", "principle": "<the principle that state follows>"}},
"milestone": "<a done-when stated as a value of the solution's metric, for example Six scripts meet every requirement>"}}. Do not use em dashes."""


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
    return ensure_solution(cue, item, spec_edit, decide)


NO_VALUE = frozenset({"", "none", "n/a", "na", "tbd", "todo", "unknown", "null", "...", "?"})
NON_VERB_START = frozenset({"the", "a", "an", "we", "it", "this", "that", "our", "i", "you", "they", "he", "she", "team", "ai"})


def field_text(value: Any) -> str:
    """A model's field as one clean line, or "" when it is empty or placeholder text (an unfilled <...> slot, none, n/a, tbd)."""
    text = " ".join(str(value or "").replace("\u2014", ",").split())
    if text.casefold().strip(" .") in NO_VALUE or (text.startswith("<") and text.endswith(">")):
        return ""
    return text


def approach_text(value: Any) -> str:
    """The approach sentence when it starts with a verb as far as code can tell (it does not open with an article or a pronoun), else ""."""
    text = field_text(value)
    return "" if not text or text.split()[0].casefold() in NON_VERB_START else text


def ensure_solution(cue: Any, item: Mapping[str, Any], spec_edit: Any, decide: Callable[[str], str]) -> Optional[Dict[str, Any]]:
    """Creates the solution for this kind of deliverable, complete as far as the model's one structured call (fast tier) gave valid fields: the solution with
    its approach and milestone, and its problem and ideal as records of their own linked by id, all through ``spec_edit.apply(record_add)``. A missing or
    placeholder field is not written, never made up by code. ``{"id", "row", "created"}``, or None when no kind was named or the solution could not be made."""
    records = str(getattr(cue, "records_text", "") or "")[:RECORD_CHARS]
    data = parse_json(decide(KIND_PROMPT.format(item=item.get("name"), milestone=cue.name, records=f"The milestone's own records:\n{records}\n" if records else "")))
    kind = field_text(data.get("name") or data.get("kind")).strip(" .")
    if not kind:
        return None
    problem = data.get("problem") if isinstance(data.get("problem"), Mapping) else {}
    ideal = data.get("ideal") if isinstance(data.get("ideal"), Mapping) else {}

    def add(table: str, key: str, fields: Mapping[str, str]) -> Dict[str, Any]:
        answer = spec_edit.apply({"edit": {"kind": "record_add", "table": table, "key": key, "fields": dict(fields)}})
        return dict(answer) if answer.get("applied") and answer.get("target") else {}
    links: Dict[str, List[str]] = {}
    what = field_text(problem.get("what"))
    if what:
        made = add("problems", what, {k: v for k, v in {"problem": what, "who_has_it": field_text(problem.get("who")), "why_it_matters": field_text(problem.get("why"))}.items() if v})
        if made:
            links["problem_ids"] = [str(made["target"])]
    ideal_name, principle = field_text(ideal.get("name")), field_text(ideal.get("principle"))
    if ideal_name:
        fields = {"name": ideal_name, **({"principle": principle} if principle else {}), **({"problem_ids": links["problem_ids"]} if links.get("problem_ids") else {})}
        made = add("ideals", ideal_name, fields)
        if made:
            links["ideal_ids"] = [str(made["target"])]
    name = kind[:1].upper() + kind[1:]
    fields = {"name": name}
    for column, value in (("purpose", field_text(data.get("purpose"))), ("approach", approach_text(data.get("approach"))), ("milestones", field_text(data.get("milestone")))):
        if value:
            fields[column] = value
    fields.update(links)
    answer = spec_edit.apply({"edit": {"kind": "record_add", "table": "solutions", "key": name, "fields": fields}})
    sid = str(answer.get("target") or "")
    if not answer.get("applied") or not sid:
        return None
    return {"id": sid, "row": fields, "created": bool(answer.get("created", True))}
