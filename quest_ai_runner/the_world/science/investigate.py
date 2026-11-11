"""Investigating for a person's quest: what a request needs from outside, looked into as a small review and handed back as a record.

The look is the hub's Learning solution (``the-learning``, its ``review``): a protocol fixed before searching, queries with dates and counts, screening
with a reason for each count, graded items, a synthesis and the limits. This module is Science's side of it for Quest: it decides, from structured facts and
one fast-tier judgement each (never from words in a message), WHICH strategy the question bears on, whether the question stays inside an experiment's
scope, and whether an earlier investigation already answers it; it carries the review out; and it turns the hub's record into the shape the Quest API
stores. Nothing here writes to Quest: the tool in ``the_body/hands/quest_operations/tools_quest_investigations.py`` records the result.

Every model call here is the fast tier (``tier="fast"``), through the hub's ``quest-world`` seat, so a deployment's tier table decides the model and nothing
is named here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_self.integrity.prompts.prompt_specs import declare, render_prompt

SIZES = ("quick", "standard", "full")
FRESH = "fresh"

PLACE_PROMPT = """You place one question that Quest AI is about to look into for a person's quest.

The question: {question}
The decision it informs: {decision}

The strategies of the quest (id: name; the first one, whose id is the quest's own id, is the whole quest):
{strategies}

The experiment it is done inside (empty when none):
{experiment}

Decide three things and answer with one JSON object and nothing else:
- "solution_ids": the ids of the NARROWEST strategies the question bears on (one or several), copied exactly from the list. Use the whole quest's id only when no narrower strategy fits.
- "within_scope": true when no experiment is given, or when the question is inside the experiment's in-scope list, is not outside its scope and does not reopen anything it holds fixed. false otherwise.
- "scope_note": one short plain sentence saying why when within_scope is false, else an empty string.
Write no em dashes."""

REUSE_PROMPT = """Quest AI is about to look into a question for a person's quest. Earlier investigations on the same strategy or one above it are listed.

The question: {question}
The decision it informs: {decision}

Earlier investigations (id | date | how sure | question | what it found):
{earlier}

Answer with one JSON object and nothing else:
- "answered_by": the id of the ONE earlier investigation whose findings answer this question, copied exactly, or an empty string when none does. An investigation about a related but different question does not answer it.
Write no em dashes."""

PLACE_SPEC = declare("investigate.place", solution="web-search", metric="metric-search-the-web", tier="fast", cache_layer="none",
                     template=PLACE_PROMPT, output_schema=None)
REUSE_SPEC = declare("investigate.reuse", solution="web-search", metric="metric-search-the-web", tier="fast", cache_layer="none",
                     template=REUSE_PROMPT, output_schema=None)

SURE_WORDS = {"A": "well established", "B": "fairly sure", "C": "not very sure", "D": "weak"}


def sure_words(grade: Optional[str]) -> str:
    """How sure a result is, in plain words, from its best grade."""
    return SURE_WORDS.get(grade or "", "nothing reliable was found")


KIND_WORDS = {"research": ("study", "studies"), "reputable": ("established publication", "established publications"), "guide": ("guide", "guides"),
              "vendor": ("company page", "company pages"), "personal": ("person's account", "people's accounts")}
IN_FULL = ("opened", "cross-checked", "observed")


def evidence_line(kinds: Sequence[str], read_in_full: bool = False) -> str:
    """What a finding rests on, in plain words, never a verdict: "1 study", "3 guides agree", "1 person's account", "a company page", "1 study and 2 guides agree",
    with ", read in full" only when it is true. ``kinds`` is the kind of each source behind the finding (research, reputable, guide, vendor, personal; anything else
    counts as a plain source). The one wording every place that shows a finding shares (the quest card, the welcome-back card, the email and the chat note)."""
    counts: Dict[str, int] = {}
    for kind in kinds:
        counts[kind if kind in KIND_WORDS else ""] = counts.get(kind if kind in KIND_WORDS else "", 0) + 1
    if not counts:
        return ""
    parts = []
    for kind, n in counts.items():
        one, many = KIND_WORDS.get(kind, ("source", "sources"))
        parts.append("a company page" if (kind, n) == ("vendor", 1) and len(counts) == 1 else f"{n} {one if n == 1 else many}")
    text = " and ".join(parts) + (" agree" if sum(counts.values()) > 1 else "")
    return text + (", read in full" if read_in_full else "")


def finding_evidence(finding: Mapping[str, Any], verified: Any = None) -> str:
    """The evidence line of one host-written finding (``source_kind``, ``also`` as a count or ``also_kinds``) with the verification of its own source."""
    if finding.get("evidence"):
        return str(finding["evidence"])                       # the host presented it already (Quest's finding presenter): one wording, said as given
    kind = str(finding.get("source_kind") or "")
    also = finding.get("also_kinds")
    if not isinstance(also, list):
        also = [""] * int(finding.get("also") or 0)
    return evidence_line([kind] + [str(k) for k in also], verified in IN_FULL)


@dataclass
class Placement:
    solution_ids: List[str] = field(default_factory=list)
    within_scope: bool = True
    scope_note: str = ""


def parse_object(text: Any) -> Dict[str, Any]:
    """The JSON object in a model reply (fences and chatter around it are ignored); empty when there is none."""
    raw = str(text or "")
    found = re.search(r"\{.*\}", raw, re.S)
    if not found:
        return {}
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def world_ask(prompt: str) -> str:
    """One fast-tier model call through the hub's ``quest-world`` seat (a tier, never a model name)."""
    from esoi import AssemblyConfig

    from quest_ai_runner.the_self.assembler import hub
    seat = hub.child().assemble(AssemblyConfig(solutions=("quest-world",)))["quest-world"]
    return str(seat.ask(prompt, "fast") or "")


def experiment_text(experiment: Optional[Mapping[str, Any]]) -> str:
    if not experiment:
        return ""
    lines = [f"research question: {experiment.get('research_question') or experiment.get('question') or ''}"]
    for key, label in (("in_scope", "in scope"), ("outside_scope", "outside scope for now"), ("held_fixed", "held fixed until the reevaluation")):
        if experiment.get(key):
            lines.append(f"{label}: " + "; ".join(str(x) for x in experiment[key]))
    return "\n".join(lines)


def place(question: str, decision: str, solutions: Sequence[Mapping[str, Any]], experiment: Optional[Mapping[str, Any]] = None,
          ask: Optional[Callable[[str], str]] = None) -> Placement:
    """The narrowest strategies the question bears on, and whether it stays inside the experiment's scope. ``solutions``: ``{id, name}`` rows with the whole
    quest first. The ids a model returns are kept only when they are in the list, and an empty answer means the whole quest. A scope that cannot be
    checked counts as outside it: an experiment's held choices are never reopened on a guess."""
    known = {str(s["id"]): str(s.get("name") or "") for s in solutions}
    whole = str(solutions[0]["id"]) if solutions else ""
    prompt = render_prompt("investigate.place", question=question, decision=decision or "(none stated)",
                           strategies="\n".join(f"{i}: {n}" for i, n in known.items()), experiment=experiment_text(experiment) or "(none)")
    data = parse_object((ask or world_ask)(prompt))
    ids = [str(s) for s in dict.fromkeys(data.get("solution_ids") or []) if str(s) in known]
    if not data:
        return Placement([whole] if whole else [], within_scope=experiment is None, scope_note="" if experiment is None else "The scope could not be checked.")
    scope = data.get("within_scope") is True or experiment is None
    return Placement(ids or ([whole] if whole else []), scope, "" if scope else str(data.get("scope_note") or "It is outside what the experiment looks into now."))


def reuse_line(row: Mapping[str, Any]) -> str:
    found = "; ".join(str(x) for x in (row.get("known") or [])[:2]) or "; ".join(str(i.get("claim")) for i in (row.get("items") or [])[:2])
    return f"{row.get('investigation_id')} | {row.get('done_on')} | {sure_words(row.get('best_grade'))} | {row.get('question')} | {found}"


def answering(question: str, decision: str, earlier: Sequence[Mapping[str, Any]], ask: Optional[Callable[[str], str]] = None) -> Optional[Mapping[str, Any]]:
    """The earlier investigation that answers the question, or None. Only ids in ``earlier`` count."""
    if not earlier:
        return None
    prompt = render_prompt("investigate.reuse", question=question, decision=decision or "(none stated)", earlier="\n".join(reuse_line(r) for r in earlier))
    chosen = str(parse_object((ask or world_ask)(prompt)).get("answered_by") or "").strip()
    return next((r for r in earlier if r.get("investigation_id") == chosen), None)


def hub_learning():
    """The hub's Learning seat, built from QAR's selections (``quest_ai_runner.run`` runs only its entry, ``stale_areas``)."""
    from esoi import AssemblyConfig

    from quest_ai_runner.the_self.assembler import hub
    return hub.child().assemble(AssemblyConfig(solutions=("the-learning",)))["the-learning"]


def review(question: str, decision: str, size: str, research_question_id: str = "", criteria: Optional[Mapping[str, Any]] = None,
           queries: Sequence[str] = (), today: str = "", learning: Any = None, solution_ids: Sequence[str] = ()) -> Mapping[str, Any]:
    """Carry out the investigation as the hub's review: sized by ``size`` (quick, standard or full; the caller never passes full unless the person
    asked), screened by ``criteria``, with the model-written synthesis only above the quick size. The hub files an investigation under at least one
    solution: the quest's strategies named here go in as its ``solution:<id>`` links (so it adds no fallback note to the limitations). The reuse of an
    earlier investigation is Quest's own (``place`` and ``answering``, over the quest's records), so the hub's lookup in its own records is off."""
    seat = learning if learning is not None else hub_learning()
    return seat.review(question, decision, size=size, criteria=dict(criteria or {}), queries=list(queries), experiment_id=research_question_id,
                       today=today or date.today().isoformat(), use_model=size != "quick", links=[f"solution:{s}" for s in solution_ids], reuse=False)


def lines_of(cell: Any) -> List[str]:
    return [x.strip() for x in str(cell or "").split(";") if x.strip()]


def quest_record(hub_record: Mapping[str, Any], *, solution_ids: Sequence[str], research_question_id: str = "", goal_id: str = "",
                 asked_by: str = "person", origin: str = "chat", extends: str = "") -> Dict[str, Any]:
    """The hub's investigation record as the body the Quest API stores. Software-only parts of the hub's record (a licence per item, a knowledge id) are
    left out, and so is the hub's applied part: what a finding changes in a person's quest is decided afterwards, through the usual confirmation."""
    row = dict(hub_record.get("investigation") or {})
    known, contested, unknown = (lines_of(row.get(k)) or [str(row.get(k) or "")] for k in ("synthesis_known", "synthesis_contested", "synthesis_unknown"))
    items = [{"item_id": i.get("item_id"), "claim": i.get("claim"), "source": i.get("source"), "source_date": i.get("source_date"),
              "independent": i.get("independent") or "unknown", "verified": i.get("verified") or "snippet", "grade": i.get("grade") or "C"}
             for i in hub_record.get("items") or []]
    found = str(row.get("found") or "0")
    record: Dict[str, Any] = {
        "solution_ids": list(solution_ids), "decision": row.get("decision") or "", "research_question_id": research_question_id or None,
        "goal_id": goal_id or None, "extends_investigation_id": extends or None, "asked_by_kind": asked_by, "carried_out_by": "quest_ai", "origin": origin,
        "kind": row.get("kind") or "review", "size": row.get("size") or "quick", "protocol_date": row.get("protocol_date") or None,
        "done_on": row.get("done_on") or None, "method": row.get("method") or "", "criteria": row.get("criteria") or "",
        "sources": [s.strip() for s in str(row.get("sources") or "").split(",") if s.strip()],
        "queries": [{"source": q.get("source"), "query": q.get("query"), "date": q.get("date"), "hits": q.get("hits") or 0} for q in hub_record.get("queries") or []],
        "found": int(found) if found.isdigit() else 0,
        "screened_out": [{"count": n, "reason": reason} for reason, n in (hub_record.get("screened_out") or {}).items()],
        "items": items, "synthesis": {"known": [x for x in known if x], "contested": [x for x in contested if x], "unknown": [x for x in unknown if x]},
        "limitations": [str(row.get("limitations") or "")] if row.get("limitations") else [],
    }
    return {k: v for k, v in record.items() if v not in (None, "")}


def summary_text(recorded: Mapping[str, Any], reused: bool = False) -> str:
    """What was looked into, how sure it is and what it changed, in plain words, for the reply."""
    known = "; ".join((recorded.get("synthesis") or {}).get("known") or [])
    if isinstance(recorded.get("findings"), list):
        known = "; ".join(f"{f.get('claim')} ({finding_evidence(f)}; {domain_of(f.get('source'))})" for f in recorded["findings"][:NOTE_FINDINGS] if f.get("claim")) or "nothing worth keeping was found"
    elif not known:
        # A quick review carries no written synthesis: the graded findings themselves are what was found, each with how sure it is and where it came from.
        found_items = [f"{i.get('claim')} ({evidence_line(['']) if i.get('source') else 'no source'}; {i.get('source')})" for i in (recorded.get("items") or [])[:6] if i.get("claim")]
        known = "; ".join(found_items) or "nothing was settled"
    unknown = "; ".join((recorded.get("synthesis") or {}).get("unknown") or [])
    limits = "; ".join(recorded.get("limitations") or [])
    lead = "An earlier investigation already answers this" if reused else "Looked into"
    queries = len(recorded.get("queries") or [])
    return (f"{lead}: {recorded.get('question')}. " + ("" if reused else f"It ran {queries} search(es) and kept {len(recorded.get('items') or [])} finding(s). ")
            + f"Found: {known}." + (f" Not established: {unknown}." if unknown else "")
            + (f" Limits: {limits}." if limits else "") + " It has changed nothing yet; any change to the person's quest is proposed and goes through their confirmation."
            + " If the person also asked for a note, call add_quest_note: the system writes its text from this record (the findings, what each rests on, where each came from), so pass a short line "
              "and do not write the findings yourself. The note is not saved until that call has run; the reply says what the note holds.")


NOTE_FINDINGS = 5
NOTE_CLAIM_CHARS = 170


def domain_of(source: Any) -> str:
    """The site a finding came from, as a reader says it ("nccih.nih.gov"), or the source text itself when it is not a link."""
    text = str(source or "").strip()
    found = re.match(r"https?://(?:www\.)?([^/\s]+)", text)
    return found.group(1) if found else text


def clip_words(text: Any, limit: int) -> str:
    """``text`` on one line, cut at a word with an ellipsis, no em or en dashes."""
    line = " ".join(str(text or "").replace("\u2014", ",").replace("\u2013", ",").split())
    if len(line) <= limit:
        return line
    return line[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def research_note(recorded: Mapping[str, Any]) -> str:
    """The note a research ask leaves on the quest, composed by code from the investigation record: its question, how sure it is, the findings each with where it came from,
    what is not established and what was read as a snippet only. When the host wrote findings (``findings``: complete sentences a person reads cold, graded by the kind of
    source, duplicates merged) the note says those and nothing else; without them it falls back to the review's items, never a cut-off snippet. "" when there is no finding.
    Short and readable; the planner does not write it."""
    item_list = list(recorded.get("items") or [])
    items = {i.get("item_id"): i for i in item_list if i.get("item_id")}
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    if isinstance(recorded.get("findings"), list):
        rows = [{"claim": f.get("claim"), "grade": f.get("grade"), "source": f.get("source") or (items.get(f.get("item_id")) or {}).get("source"), "also": f.get("also") or 0,
                 "verified": (items.get(f.get("item_id")) or {}).get("verified"), "evidence": finding_evidence(f, (items.get(f.get("item_id")) or {}).get("verified"))}
                for f in recorded["findings"] if f.get("claim")]
    else:
        rows = [{"claim": i.get("claim"), "grade": i.get("grade"), "source": i.get("source"), "also": 0, "verified": i.get("verified"), "evidence": evidence_line([""], i.get("verified") in IN_FULL)} for i in item_list
                if i.get("claim") and "..." not in str(i["claim"]) and "\u2026" not in str(i["claim"]) and str(i["claim"]).strip()[-1:] in ".!?"]      # an earlier record: only a finding already written whole
    seen, picked = set(), []
    for row in sorted(rows, key=lambda r: order.get(str(r.get("grade")), 4)):
        key = re.sub(r"[^a-z0-9]+", " ", str(row["claim"]).lower())[:50]
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= NOTE_FINDINGS:
            break
    if not picked:
        return ""
    lines = [f"Research note: {clip_words(recorded.get('question'), 200)}", "Findings:"]
    for row in picked:
        lines.append(f"- {' '.join(str(row['claim']).split())} ({row['evidence']}; {domain_of(row.get('source'))})")
    unknown = [re.sub(r"^not established:\s*", "", str(u).strip(), flags=re.IGNORECASE) for u in ((recorded.get("synthesis") or {}).get("unknown") or [])
               if str(u).strip() and not re.match(r"\s*(?:not established:\s*)?licen[cs]e\b", str(u), re.IGNORECASE)]
    if unknown:
        lines.append(f"Not established: {clip_words('; '.join(unknown), 220)}")
    snippets = [r for r in picked if r.get("verified") in (None, "snippet", "not-opened")]
    if snippets:
        full = len(picked) - len(snippets)
        lines.append(f"Limits: {full} of {len(picked)} findings {'was' if full == 1 else 'were'} read in full; the {'others come' if len(snippets) > 1 else 'other comes'} from search results.")
    return "\n".join(lines).replace("\u2014", ",").replace("\u2013", ",")


def research_reply(note: str) -> str:
    """The reply for a research turn whose note was saved, written by code from that note so the two cannot disagree: what was looked into, how sure it is, the first findings and
    what is not established. "" when the note is not one ``research_note`` composed."""
    lines = [ln for ln in str(note or "").splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("Research note: "):
        return ""
    question = lines[0][len("Research note: "):].rstrip("?").strip()
    findings = [ln for ln in lines if ln.startswith("- ")][:3]
    unknown = next((ln for ln in lines if ln.startswith("Not established: ")), "")
    parts = [f"I looked into this and saved a note on the quest: {question}.", "What the note holds first:\n" + "\n".join(findings) if findings else "", unknown]
    return "\n\n".join(p for p in parts if p)
