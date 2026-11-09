"""Each ESOI solution a deep goal touches gets its own consideration, through the hub (QAR's wiring of hubofhubs ``relevant-solutions`` and
``solution-passes``).

For a goal whose working folder holds ESOI records (an ``esoi.toml`` at or above it, or ``QAR_SPEC_ROOT``): the hub names the solutions the
goal concerns and forms each one's prompt from its own network of records with only the knowledge that bears on the goal; one cheap pass per
solution says what it needs, the target its own metric sets, the risks and any conflict with its recorded results; narrower passes judge each
scope above and each solution relying on it. The deep agent's brief then carries each touched solution's generated prompt and what its pass
found, so the worker reads that solution's memory before general knowledge.

After a milestone step (``after_milestone``), the milestone's own solution is not passed again: its finding is the measured step itself
(``milestone_progress.milestone_pass``), and only its scopes above and its dependents get passes, when the milestone's solution is in records this
lane can read. Everything here is bounded by a time limit and never raises: no records, no provider or a slow pass is an empty block.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

log = logging.getLogger("quest-ai-runner.solution-consideration")

TIMEOUT_SECONDS = 120.0      # a deep goal's consideration: the passes run in parallel, so this bounds the slowest level of passes
MILESTONE_SECONDS = 45.0     # after a milestone step the person is waiting for the reply: the scopes above get less time
PROMPT_CHARS = 12000         # each touched solution's generated prompt in the brief, cut at a line beyond this
TIER = "fast"                # the passes are many small calls: the cheap tier, resolved by the tier chooser


def ask_for(provider: Any, tier: str = TIER) -> Optional[Callable[[str], str]]:
    """A one-argument model call at ``tier``, or None with no provider."""
    if provider is None:
        return None
    from quest_ai_runner.the_consciousness.representatives.personas import _judge_model
    model = _judge_model(provider, tier)

    def ask(prompt: str) -> str:
        return provider.answer([{"role": "user", "content": prompt}], model=model) or ""
    return ask


def bounded(work: Callable[[], Any], seconds: float) -> Any:
    """``work()`` within ``seconds``, else None (the thread is left to finish on its own); an error is logged and is None."""
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except Exception:      # noqa: BLE001  consideration never fails the goal it informs
            log.exception("solution consideration failed")
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return box.get("value")


ROW_TABLES = {"intents": "intent_id", "problems": "problem_id", "ideals": "ideal_id", "solutions": "solution_id",
              "implementations": "implementation_id", "metrics": "metric_id", "knowledge": "knowledge_id"}
QUEST_NETWORKS: Dict[str, Any] = {}


def cell(value: Any) -> str:
    return ";".join(str(v) for v in value) if isinstance(value, (list, tuple)) else ("" if value is None else str(value))


def quest_rows(tables: Mapping[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """The quest's specification records (``{table: {record id: row}}`` as Quest returns them) as the rows the hub's network reads."""
    rows: Dict[str, List[Dict[str, str]]] = {}
    for table, id_column in ROW_TABLES.items():
        held = tables.get(table) or {}
        items = held.items() if isinstance(held, Mapping) else [(str(r.get(id_column, "")), r) for r in held]
        rows[table] = [{**{k: cell(v) for k, v in dict(row).items()}, id_column: str(rid)} for rid, row in items]
    return {k: v for k, v in rows.items() if v}


def quest_network(quest_id: str, tables: Mapping[str, Any]) -> Any:
    """The hub's network of a quest's records, built from rows (no folder) and kept until the records change."""
    import hashlib
    import json as json_module
    from hubofhubs.api import SolutionNetwork
    rows = quest_rows(tables)
    key = quest_id + ":" + hashlib.sha256(json_module.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    if key not in QUEST_NETWORKS:
        QUEST_NETWORKS.clear()
        QUEST_NETWORKS[key] = SolutionNetwork.from_rows(rows, name=f"quest {quest_id}")
    return QUEST_NETWORKS[key]


def cut(text: str, chars: int = PROMPT_CHARS) -> str:
    if len(text) <= chars:
        return text
    return text[:chars].rsplit("\n", 1)[0] + "\n(cut for length: read the rest with `esoi spec prompt <solution>`)"


def finding_lines(finding: Mapping[str, Any]) -> List[str]:
    target = finding.get("target") or {}
    lines = [f"- Needs: {finding.get('needs') or 'nothing from this goal'}"]
    if target.get("metric_id") or target.get("done_when"):
        lines.append(f"- Target from its own metric: {target.get('metric_id') or ''} {target.get('value') or ''}. Done when: {target.get('done_when') or ''}".rstrip())
    lines += [f"- Risk: {r}" for r in finding.get("risks") or []]
    lines += [f"- Conflict with its records: {c.get('record', '')}: {c.get('conflict') or c.get('says') or ''}" if isinstance(c, Mapping) else f"- Conflict: {c}"
              for c in finding.get("conflicts") or []]
    if finding.get("error"):
        lines.append(f"- Its pass did not answer: {finding['error']}")
    return lines


def render(briefs: Mapping[str, Any], considered: Mapping[str, Any]) -> str:
    """The brief block: each touched solution's generated prompt and its finding, then the scopes above, the dependents and what was cut."""
    findings = {f["solution_id"]: f for f in considered.get("findings") or []}
    parts = ["## Each ESOI solution this goal touches, with its own records\n"
             "Each solution below was given its own consideration. Read its records before general knowledge; a recorded result outranks a "
             "reputation. Hold the part of the work for a solution to that solution's own target."]
    for brief in briefs.get("solutions") or []:
        sid = brief["solution_id"]
        parts.append(f"### {brief.get('name') or sid} ({sid})\nWhat its own pass found:\n" + "\n".join(finding_lines(findings.get(sid, {})))
                     + "\n\nIts records:\n" + cut(str(brief.get("prompt") or "")))
    scopes = [f"- {p.get('name') or p['solution_id']} (level {p.get('level')}): combination affected {p.get('combination_effect')}; "
              f"{p.get('metric_update') or 'no metric update'}; {p.get('why') or ''}" for p in considered.get("parents") or []]
    if scopes:
        parts.append("### The scopes above\n" + "\n".join(scopes))
    relying = [f"- {d.get('name') or d['solution_id']} relies on {', '.join(d.get('relies_on') or [])}: still holds {d.get('still_holds')}. {d.get('why') or ''}"
               for d in considered.get("dependents") or []]
    if relying:
        parts.append("### Solutions relying on these\n" + "\n".join(relying))
    if considered.get("cut"):
        parts.append("### Not considered (cap)\n" + "\n".join(f"- {c['solution_id']}: {c['why']}" for c in considered["cut"]))
    return "\n\n" + "\n\n".join(parts) + "\n"


def consider(request: str, root: Any, ask: Callable[[str], str], found: Optional[List[Mapping[str, Any]]] = None,
             own_passes: Optional[Mapping[str, Callable[..., Mapping[str, Any]]]] = None,
             on_finding: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], Any]] = None) -> Dict[str, Any]:
    """The hub's flow for one request over the records at ``root``: relevant solutions (or ``found``), their briefs, their passes.
    ``on_finding(finding, brief)`` is called for each relevant solution as soon as its own pass returns, before the parent passes."""
    from hubofhubs.api import NetworkPasses, RecordsIndex, briefs, network_of
    records = root if hasattr(root, "classify") else str(root)
    solutions = list(found) if found is not None else list(RecordsIndex().relevant(request, records)["solutions"])
    if not solutions:
        return {}
    prepared = briefs(network_of(records), solutions, request)
    by_id = {b["solution_id"]: b for b in prepared.get("solutions") or []}
    hook = (lambda finding: on_finding(finding, by_id.get(finding["solution_id"], {}))) if on_finding is not None else None
    return {"briefs": prepared, "considered": dict(NetworkPasses(None).consider(request, prepared, own_passes=own_passes, ask=ask, on_finding=hook))}


def brief_block(request: str, working_dir: Optional[str], provider: Any, status: Optional[Callable[[str], None]] = None,
                seconds: float = TIMEOUT_SECONDS) -> str:
    """The block for a deep goal's brief ("" when the folder holds no ESOI records, there is no provider, or it ran out of time)."""
    try:
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.repo_specs import slice_root
        root = slice_root(working_dir)
        ask = ask_for(provider)
        if root is None or ask is None:
            return ""
        if status is not None:
            status("Reading what each part of the design this touches already knows.")
        got = bounded(lambda: consider(request, root, ask), seconds)
        return render(got["briefs"], got["considered"]) if got else ""
    except Exception:      # noqa: BLE001
        log.exception("solution consideration block failed")
        return ""


def after_milestone(made: Any, cue: Any, request: str, provider: Any, seconds: float = MILESTONE_SECONDS) -> str:
    """Lines for the reply after a milestone step: the scopes above the milestone's solution and its dependents, judged with the measured step as
    that solution's own finding. "" when the milestone's solution is not in records this lane can read, or on any failure."""
    try:
        from hubofhubs.api import network_of
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_progress import milestone_pass
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.repo_specs import slice_root
        root: Any = slice_root(getattr(cue, "working_dir", None))
        sid = str(getattr(cue, "rid", "") or "")
        ask = ask_for(provider)
        if root is None and getattr(cue, "tables", None):
            root = quest_network(str(getattr(cue, "quest_id", "") or ""), cue.tables)      # the quest's own records, as Quest holds them
        if root is None or not sid or ask is None:
            return ""
        network = network_of(root if hasattr(root, "classify") else str(root))
        if not network.has(sid):
            return ""
        finding = milestone_pass(made, cue, request)
        found = [{"solution_id": sid, "name": network.name(sid), "score": 0.0, "via": [], "chain": network.chain(sid), "dependents": network.dependents(sid)}]
        got = bounded(lambda: consider(request, root, ask, found, {sid: lambda req, brief: finding}), seconds)
        if not got:
            return ""
        considered = got["considered"]
        lines = [f"{p.get('name') or p['solution_id']}: {p.get('why') or ''}".strip() for p in considered.get("parents") or []
                 if p.get("combination_effect") or p.get("metric_update")]
        lines += [f"{d.get('name') or d['solution_id']} may no longer get what it relies on: {d.get('why') or ''}".strip()
                  for d in considered.get("dependents") or [] if d.get("still_holds") is False]
        return "\n".join(lines)
    except Exception:      # noqa: BLE001
        log.exception("after-milestone consideration failed")
        return ""


# -- chat turns --------------------------------------------------------------------------------------------------------------------

CARD_OWNER = "solution-consideration"
CARD_CHARS = 6000
CARD_LOCK = threading.Lock()
FAILURES = {"count": 0, "said": False}      # chat-turn considerations that failed in this process; the first is said as a warning


def failed(ctx_meta: Dict[str, Any], error: BaseException) -> None:
    """A consideration that failed is never silent: the turn's meta says so (``solution_consideration``), the process says it once as a
    warning (a missing library or records cannot fail quietly on every turn) and counts every failure after that."""
    with CARD_LOCK:
        FAILURES["count"] += 1
        first = not FAILURES["said"]
        FAILURES["said"] = True
    ctx_meta["solution_consideration"] = {"status": "failed", "error": f"{type(error).__name__}: {error}"[:300], "failures_in_process": FAILURES["count"]}
    if first:
        log.warning("Solution consideration failed and will be counted on every turn after this one: %s: %s", type(error).__name__, error, exc_info=error)
    else:
        log.info("Solution consideration failed (%d in this process): %s: %s", FAILURES["count"], type(error).__name__, error)


def solution_card(brief: Mapping[str, Any], finding: Mapping[str, Any], considered: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """One touched solution as a managed card of the turn's card store (QAR's adapter of the hub's managed solution card): its generated
    prompt and what its own pass found, rebuilt by this module only."""
    sid = str(brief["solution_id"])
    name = str(brief.get("name") or sid)
    above = [f"- {p.get('name') or p['solution_id']}: {p.get('why') or ''}" for p in (considered or {}).get("parents") or []
             if sid in descendants_of(p, considered or {})]
    note = ("What its own pass found:\n" + "\n".join(finding_lines(finding)) + (("\nThe scopes above found:\n" + "\n".join(above)) if above else "")
            + "\n\nIts records:\n" + cut(str(brief.get("prompt") or ""), CARD_CHARS))
    return {
        "id": f"solution-{sid}", "name": f"Solution: {name}", "summary": f"Solution: {name}",
        "description": f"The ESOI solution {name} ({sid}): its own records and its consideration of the latest request that touched it.",
        "keywords": sorted({w for w in (name + " " + sid).lower().replace("-", " ").split() if len(w) > 2} | {"solution"}),
        "conventions": [], "files": [],
        "content": [{"id": "solution-consideration", "type": "note", "why": "This solution's records and its own pass on the request",
                     "locator": {"text": note}}],
        "scope_tags": [], "managed_by": CARD_OWNER, "managed_fields": ["name", "summary", "description", "keywords"],
        "managed_items": ["solution-consideration"],
        "provenance": {"created_by_task": "solution consideration", "model": "", "created_at": "", "last_verified_at": ""},
    }


def descendants_of(parent: Mapping[str, Any], considered: Mapping[str, Any]) -> List[str]:
    """The solutions under a parent pass, through the levels between."""
    from hubofhubs.api import descendants
    return descendants(parent["solution_id"], list(considered.get("parents") or []))


def write_cards(store: Any, got: Mapping[str, Any]) -> List[str]:
    """Writes each touched solution's card; returns the ids written."""
    findings = {f["solution_id"]: f for f in (got.get("considered") or {}).get("findings") or []}
    written: List[str] = []
    for brief in (got.get("briefs") or {}).get("solutions") or []:
        card = solution_card(brief, findings.get(brief["solution_id"], {}))
        try:
            if store.write_card(card["id"], card):
                written.append(card["id"])
        except Exception:      # noqa: BLE001  one card that cannot be written does not stop the others
            log.exception("solution card not written: %s", card["id"])
    return written


def for_turn(orch: Any, rs: Any, ctx_meta: Optional[Dict[str, Any]]) -> Optional[threading.Thread]:
    """A chat turn that touches ESOI solutions: each solution gets its own consideration in a background thread started after the
    acknowledgement, in parallel with context assembly and the planner. Each relevant solution's card (its generated prompt and its own
    pass's finding) is written the moment its pass returns, before the parent passes, and its id is added to the turn's
    ``priority_card_ids`` list, so whatever reads the turn's cards after that (assembly, a mid-turn card read) puts it first. The turn never
    waits for it. A milestone turn is skipped (its own step runs ``after_milestone``). Returns the thread (for tests), never raises."""
    try:
        if ctx_meta is None or getattr(rs, "cue_resolved", None) is not None or not getattr(rs, "message_is_user_turn", True):
            return None
        from quest_ai_runner.the_body.configuration.runtime_settings import core_settings
        if not core_settings().solution_consideration_in_chat:
            return None
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.repo_specs import slice_root
        from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import card_writer
        store = card_writer(getattr(orch, "context_assembler", None))
        ask = ask_for(getattr(orch, "provider", None))
        if store is None or ask is None:
            return None
        priority = ctx_meta.get("priority_card_ids")
        if not isinstance(priority, list):
            priority = list(priority or [])
            ctx_meta["priority_card_ids"] = priority       # one list, shared with every copy of the turn's meta, so a card added later is seen
        folder = str(ctx_meta.get("quest_folder") or "") or None
        quest_id = str(getattr(rs, "quest_id", "") or "")
        tools = getattr(orch, "tools", None)
        request = str(getattr(rs, "goal_condition", "") or getattr(rs, "user_message", ""))

        def landed(finding: Mapping[str, Any], brief: Mapping[str, Any]) -> None:
            if not brief:
                return
            card = solution_card(brief, finding)
            if store.write_card(card["id"], card):
                with CARD_LOCK:
                    if card["id"] not in priority:
                        priority.append(card["id"])

        def run() -> None:
            began = time.monotonic()
            try:
                root: Any = slice_root(folder)
                if root is None and quest_id and tools is not None:
                    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_cue import read_through
                    tables, _ = read_through(tools, quest_id)       # the quest's own records, through the backend API
                    root = quest_network(quest_id, tables) if tables else None
                if root is None:
                    ctx_meta["solution_consideration"] = {"status": "no records"}
                    return
                if root is not None:
                    got = consider(request, root, ask, on_finding=landed)
                    ctx_meta["solution_consideration"] = {"status": "done", "cards": list(priority), "seconds": round(time.monotonic() - began, 1)}
                    log.info("Solution consideration: %d solution card(s) for %s in %.1fs", len((got.get("briefs") or {}).get("solutions") or []),
                             getattr(root, "root", root), time.monotonic() - began)
                    findings = {f["solution_id"]: f for f in (got.get("considered") or {}).get("findings") or []}
                    for brief in (got.get("briefs") or {}).get("solutions") or []:      # the cards again, now with the scopes above judged
                        card = solution_card(brief, findings.get(brief["solution_id"], {}), got.get("considered") or {})
                        store.write_card(card["id"], card)
            except Exception as error:      # noqa: BLE001  never fails the turn, never silent either
                failed(ctx_meta, error)
        thread = threading.Thread(target=run, name="solution-consideration", daemon=True)
        thread.start()
        return thread
    except Exception:      # noqa: BLE001
        log.exception("chat-turn solution consideration failed")
        return None
