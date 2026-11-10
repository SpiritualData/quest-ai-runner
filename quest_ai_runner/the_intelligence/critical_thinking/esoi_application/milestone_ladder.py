"""The increment ladder: decompose the milestone into work items, pick the one the AI does now, write or hand it off, measure it before and after, and keep it.

Role: the flow of one progress turn, ``make_progress``, and the item steps it uses (``work_items``, ``first_person_step``, ``dependent_item``). It composes the facts, rubric and depth modules through the hub's ``CriticalThinkingComposition.progress``.
"""
from __future__ import annotations

import datetime
import hashlib

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .milestone_depth import STATUS_WORDS, marked_claims
from .milestone_depth import ANSWER_PROMPT, COMBINE_PROMPT, EVIDENCE_PROMPT, EVIDENCE_QUESTIONS, PART_PROMPT, WRITE_PROMPT, checked_line, deliverable_brief, held_source, kept_step, solution_prompt, stems5, supported_sentences, without_rubric_echo
from .milestone_completion import unmet_of, unmet_section
from .milestone_facts import DELIVERABLES, DEPTH_CALLS, GROUNDING_CHARS, KEEP_TOOL, NO_EM_DASH, OUTWARD, RECORD_CHARS, TO_BE_DECIDED, ToolSpecEdit, clip_line, kept_follow_on, kept_parts, log, parse_json, quest_metric_fields, questions_of, render_questions, squash, whole_row, with_reading_fields, without_ids
from .milestone_reply import Progress
from .milestone_rubric import DECISION_REQUIREMENT, Scorer, deliverable_home, done_digest, done_test_class, kept_artifacts, kept_rubric, pinned_items, researched_rubric, rubric_entry, with_entry


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


def next_unmet_milestone(row: Mapping[str, Any]) -> str:
    """The first milestone of the solution's ladder not yet reached: from the structured entries (``milestoneEntries``, each with ``sentence`` and ``met_at``)
    when the record carries them, else the record's ``milestones`` text ("" when it holds neither)."""
    entries = [e for e in (row.get("milestoneEntries") or row.get("milestone_entries") or []) if isinstance(e, Mapping) and str(e.get("sentence") or "").strip()]
    if entries:
        waiting = next((e for e in entries if not (e.get("met_at") or e.get("metAt"))), None)
        return " ".join(str(waiting["sentence"]).split()) if waiting else ""
    return " ".join(str(row.get("milestones") or "").split())


def implementation_in_use(tables: Mapping[str, Any], sid: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The implementation record the solution's ``selected`` cell names (a name, an implementation id or its suffix, or a file path with ``::name``); {} when
    the solution selects none or no record of its own matches."""
    selected = str(row.get("selected") or "").strip().lower()
    module, _, named = selected.partition("::")
    wanted = {w.strip() for w in (selected, module, named) if w.strip()}
    for iid, impl in dict(tables.get("implementations") or {}).items():
        if str(impl.get("solution_id") or "") != sid:
            continue
        names = {str(impl.get("name") or "").strip().lower(), str(iid).strip().lower(), str(iid).partition(":")[2].strip().lower(), str(impl.get("filepath") or "").strip().lower()}
        if wanted & (names - {""}):
            return impl
    return {}


#: The kind of work a milestone deliverable is for the harness cascade: the runner that executes it starts on the tier the hub measured best for it.
WORK_KIND = "hard"


def make_progress(cue: Any, registry: Any, decide: Callable[[str], str], grounding: str = "", today: Optional[datetime.date] = None,
                  write: Optional[Callable[[str], str]] = None, research: Optional[Callable[[str], str]] = None, tier: str = "",
                  deep: Optional[Callable[[str, str], Mapping[str, Any]]] = None, skill: str = "",
                  specify: Optional[Callable[[str], str]] = None) -> Progress:
    """The progress step for a resolved milestone ``cue`` (``milestone_cue.Resolved``). Raises ``milestone_cue.LaneGap`` when a read or the keep fails.
    ``decide`` is the cheap call for the structured judgments (work items, checks, scoring); ``write`` writes the deliverable's prose (the tier chosen for
    it, ``tier`` names it; ``decide`` when none); ``research(query) -> text`` reads the product's own documents and outside sources for the facts a
    deliverable needs (each passage with its source). ``deep(goal, brief) -> {"output", "deferred", "error"}`` runs work through the harness the ESOI cascade
    selected for work (the deep agent); when given, a deliverable is made there from its solution's brief, never by the cheap writer: an inline result is
    measured and kept here, a queued one (long work, a background task) is acknowledged with its before measured."""
    write = write or decide
    # Creating structure (a new solution with its problem, ideal and milestone; the ways a solution can be carried out) is specification, which later
    # work is framed by: it runs on its own tier (``specify``, the balanced tier), never on the cheap one the judgments use.
    specify = specify or decide
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
    kept_unmet = unmet_of(kept_text)        # the requirement each kept part failed at the last measure, written there by milestone_completion
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
            home = deliverable_home(cue, first, spec_edit, decide, specify=specify)
            home_row = dict((home or {}).get("row") or {})
            home_rubric = kept_rubric(home_row) if home_row else None
            out_home.update(home or {})
            if home and home.get("created") and isinstance(cue.tables, Mapping):
                cue.tables = dict(cue.tables, solutions=dict(cue.tables.get("solutions") or {}, **{home["id"]: home_row}))
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
            # A requirement that waits on a status the records do not hold ("on the agreed list" while the list is a proposal) can never be met by the
            # work: it is dropped by code, the same rule the deliverable's own sentences follow.
            first["requirements"] = [r for r in first["requirements"] if not [m for m in marked_claims(r, reference_now) if STATUS_WORDS.fullmatch(m)]] \
                or first["requirements"]
            owner = str(out_home.get("id") or "")
            entry = rubric_entry(first["name"], first["requirements"], first.get("total") if isinstance(first.get("total"), int) else None,
                                 ", ".join(cited + [f"{cue.rid}", "Quest AI", day]), kind=first["kind"], done="" if owner else digest)
            stored = kept_rubric({"knowledge": entry})
            first["requirements"] = stored.requirements if stored else first["requirements"]     # the words as stored, so a later turn's reuse scores the same rubric
            held = home_row.get("knowledge") if owner else row.get("knowledge")
            answer = spec_edit.apply({"edit": {"kind": "spec_edit", "target": owner or cue.rid, "field": "knowledge", "value": with_entry(held, entry)}})
            if answer.get("applied") and not owner:
                knowledge_now = with_entry(row.get("knowledge"), entry)
            if owner and answer.get("applied"):
                home_row["knowledge"] = with_entry(held, entry)
                if isinstance(cue.tables, Mapping):
                    cue.tables = dict(cue.tables, solutions=dict(cue.tables.get("solutions") or {}, **{owner: home_row}))
            if owner and out_home.get("created"):
                # The created solution gets its own metric: units of the deliverable meeting its rubric (the rubric said in the metric's purpose).
                spec_edit.apply({"edit": {"kind": "record_add", "table": "metrics", "key": f"{home_row.get('name') or first['name']} meeting their rubric",
                                          "fields": quest_metric_fields({"name": f"{home_row.get('name') or first['name']} meeting their rubric",
                                                                         "purpose": f"Counts the units of {home_row.get('name') or first['name']} that meet every rubric item.",
                                                                         "record": "Current value: 0", "solution_ids": [owner], "rubric": first["requirements"],
                                                                         "total": first.get("total") if isinstance(first.get("total"), int) else None,
                                                                         "unit": "units"})}})
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
    out = Progress(title=title, metric_name=metric_name, goal_exists=kept_goal is not None,
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

    def work_solution() -> str:
        """The solution the deliverable's work is carried out on: the deliverable's own solution (classified or created), else the milestone's."""
        sid = str(out_home.get("id") or "")
        return f"{(out_home.get('row') or {}).get('name') or sid} (record {sid})" if sid else f"{cue.name} (record {cue.rid})"

    def carried(overview: str) -> str:
        """``overview`` with the not-yet-met section the goal already carries, so a turn that rewrites the overview does not lose which part failed what."""
        return overview + unmet_section([{"part": n, "requirements": rs} for n, rs in sorted(kept_unmet.items())])

    def frame_text() -> str:
        """The hub's labelled frame of the solution the worker works inside, from the quest's records: its approach, the next milestone not yet met, what its
        metric cannot see, the disadvantages of the implementation in use and the last measured value. The generated solution text carries the same
        records in its own order and cut to its budget; the frame puts these few lines first and labelled. The unmet parts are not repeated here: the brief's
        repair section lists them."""
        from hubofhubs.api import solution_frame
        sid = str(out_home.get("id") or "")
        solution = dict(out_home.get("row") or {}) if sid else {}
        metric = metrics.get(cue.metric_ids[0]) if cue.metric_ids else {}
        impl = implementation_in_use(tables, sid or cue.rid, solution or dict(row))
        return solution_frame({"approach": solution.get("approach") or row.get("approach"), "next_milestone": next_unmet_milestone(row),
                               "metric_disadvantages": (metric or {}).get("disadvantages"), "implementation_disadvantages": impl.get("disadvantages"),
                               "last_value": clip_line(cue.current_text() if cue.reading else cue.value, 200)})

    def approach_text(requirements: Sequence[str]) -> str:
        """The way this run takes, chosen among distinct ways by the rubric when the solution has no implementation on record yet: the chosen way and
        the ways not taken are kept as the solution's implementation records (the chosen one selected), so the next run extends the way in use and
        never chooses again. A solution that already has an implementation costs no call here. Never fails the work: no approach is an empty line."""
        sid = str(out_home.get("id") or "")
        if not sid or not requirements or any(str(i.get("solution_id") or "") == sid for i in dict(tables.get("implementations") or {}).values()):
            return ""
        try:
            from .approach_choice import approach_block
            text, records = approach_block(frame_text() + "\n" + solution_records()[:RECORD_CHARS], requirements, specify, solution_id=sid)
            for record in records:
                fields = {k: v for k, v in record.items() if k not in ("name", "selected") and v}
                spec_edit.apply({"edit": {"kind": "record_add", "table": "implementations", "key": record["name"], "fields": fields}})
                if record.get("selected"):
                    spec_edit.apply({"edit": {"kind": "spec_edit", "target": sid, "field": "selected", "value": record["name"]}})
            return text
        except Exception:
            log.warning("approach choice failed; the work goes on without it", exc_info=True)
            return ""

    def solution_records() -> str:
        """The deliverable's solution as the hub generates its prompt from its own network of records (records, metric, knowledge), then the milestone's
        records: the writer and the deep agent work from the solution, not from an ad hoc description."""
        sid = str(out_home.get("id") or "")
        own = solution_prompt(cue, sid, str(chosen.get("item", {}).get("name") or "")) if sid else ""
        return "\n\n".join(t for t in ((f"The deliverable's own solution, from its records:\n{own}" if own else ""),
                                         "The milestone, from its records:\n" + cue.records_text[:RECORD_CHARS * 2]) if t)

    def write_unit(item: Mapping[str, Any], requirements: List[str], have: List[str], note: str = "") -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
        """One unit of a deliverable: written on the writer's tier from the records, the reads and the research, then built out by the hub's depth loop when the
        separate check finds it short of its requirements. Returns the unit, the writer's answer (its proposed units) and the depth account."""
        total = item.get("total") if isinstance(item.get("total"), int) else None
        kept_note = (f"Already kept (do not repeat; write the next unit, number {len(have) + 1}):\n" + "\n".join(f"- {clip_line(p)}" for p in have)) if have else ""
        kept_note = (kept_note + "\n" + note).strip()
        data = parse_json(write(WRITE_PROMPT.format(records=solution_records(), grounding=shown(),
                                                    item=item.get("name"), requirements="; ".join(requirements) or "(none given)",
                                                    kept=kept_note, total=total or "the", no_em_dash=NO_EM_DASH)))
        part = str(data.get("part") or "").strip()
        depth = None
        if part and requirements:
            # The unit is its own solution with the kept rubric as its metric: the hub's depth loop checks it (the separate scorer, never the writer), splits what
            # it misses into sub-pieces with their own sub-prompts, recombines and checks again within the request's budget.
            from hubofhubs.api import build_out
            prompt = WRITE_PROMPT.format(records=solution_records(), grounding=shown(),
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
        if increment.get("method") == "evidence" and deep is not None:
            # Evidence is research work too: the deep harness answers the questions (its own reads and web research, each answer with its source).
            requirements = [str(r) for r in increment.get("requirements") or []]
            before = increment.get("before")
            record = {"quest_id": cue.quest_id, "milestone_id": cue.rid, "solution_id": str(out_home.get("id") or cue.rid), "milestone_metric_id": cue.metric_ids[0] if cue.metric_ids else "", "title": title,
                      "item": item.get("name"), "requirements": requirements, "method": "evidence",
                      "before": (before.get("value") if isinstance(before, Mapping) else before) or 0, "total": None,
                      "measure_name": f"Questions for {item.get('name')} answered with a source", "metric_id": increment.get("measure_id"), "kind": WORK_KIND}
            goal, brief = deliverable_brief(item, requirements, [], title, solution_records(), shown(), record=record, form="evidence", skill=skill,
                                            solution=work_solution(), milestone=f"{cue.name} (record {cue.rid})")
            got = dict(deep(goal, brief) or {})
            out.handed_off = {"goal": goal, "queued": bool(got.get("deferred")), "error": str(got.get("error") or "")}
            if got.get("deferred") or not str(got.get("output") or "").strip():
                out.overview = f"{item.get('name')}."
                return {"artifact": None, "evidence_ref": title}
            out.handed_off = None
            from .milestone_completion import deliverable_of
            found = questions_of(deliverable_of(got.get("output")) or str(got.get("output") or "")) or {}
            questions = list(found.get("questions") or [])
            rendered = render_questions(questions) if questions else ""
            if rendered:
                checked, dropped = supported_sentences(rendered, sources_text() + "\n" + str(got.get("output") or ""), decide, keep_line_starts=True)
                out.unsupported += dropped
                questions = (questions_of(checked) or {}).get("questions") or questions
                rendered = render_questions(questions)
            out.parts = [rendered] if rendered else []
            out.overview = f"The questions the decision needs, each answered from a cited source; the rest are {TO_BE_DECIDED}."
            return {"artifact": {"questions": questions} if questions else None, "evidence_ref": title}
        if increment.get("method") == "evidence":
            return gather_evidence(item, increment, kept)
        total = item.get("total") if isinstance(item.get("total"), int) else None
        have = list((kept or {}).get("parts") or [])
        requirements = [str(r) for r in increment.get("requirements") or []]
        if deep is not None:
            # Part 3: the work runs through the harness the ESOI cascade selected for work (the deep agent), from the solution's brief. A queued run is
            # background work: acknowledged, its before measured, kept by the worker on the same goal; an inline run comes back and is measured here.
            before = increment.get("before")
            record = {"quest_id": cue.quest_id, "milestone_id": cue.rid, "solution_id": str(out_home.get("id") or cue.rid), "milestone_metric_id": cue.metric_ids[0] if cue.metric_ids else "", "title": title, "item": item.get("name"), "requirements": requirements,
                      "before": (before.get("value") if isinstance(before, Mapping) else before) or 0, "total": total,
                      "measure_name": f"Units of {item.get('name')} meeting their requirements", "metric_id": increment.get("measure_id"), "kind": WORK_KIND}
            goal, brief = deliverable_brief(item, requirements, have, title, solution_records(), shown(), record=record, skill=skill,
                                            solution=work_solution(), milestone=f"{cue.name} (record {cue.rid})", frame="\n".join(t for t in (frame_text(), approach_text(requirements)) if t),
                                            unmet=kept_unmet)
            got = dict(deep(goal, brief) or {})
            out.handed_off = {"goal": goal, "queued": bool(got.get("deferred")), "error": str(got.get("error") or "")}
            if got.get("deferred") or not str(got.get("output") or "").strip():
                out.parts, out.overview = have, carried(f"{item.get('name')}.")
                return {"artifact": None, "evidence_ref": title}
            out.handed_off = None
            part, data, depth = cleaned(str(got.get("output") or "")), {}, None
        else:
            part, data, depth = write_unit(item, requirements, have)
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
        out.overview = carried(head + (("\nProposed units (an AI proposal from the sources, to be agreed):\n" + proposed) if proposed else ""))
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

    out.status = "handed_off" if out.handed_off and out.handed_off.get("queued") else str(made.get("status") or "")
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
    if out.status == "handed_off":
        out.kept_artifacts = kept_artifacts(spec_edit.written, knowledge_start, metrics)
        out.before = (made.get("before") or {}).get("value") if made.get("before") else out.before
        return out
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


def dependent_item(items: Sequence[Dict[str, Any]], name: str, doable: Callable[[Mapping[str, Any]], bool]) -> Optional[Dict[str, Any]]:
    """The AI-doable deliverable whose ``depends_on`` names ``name`` (case and spacing aside), else None."""
    key = " ".join(name.lower().split())
    return next((i for i in items if str(i.get("kind") or "") == "deliverable" and doable(i) and " ".join(str(i.get("name") or "").lower().split()) != key
                 and key and " ".join(str(i.get("depends_on") or "").lower().split()) == key), None)
