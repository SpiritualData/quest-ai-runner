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
             own_passes: Optional[Mapping[str, Callable[..., Mapping[str, Any]]]] = None) -> Dict[str, Any]:
    """The hub's flow for one request over the records at ``root``: relevant solutions (or ``found``), their briefs, their passes."""
    from hubofhubs.api import NetworkPasses, RecordsIndex, briefs, network_of
    solutions = list(found) if found is not None else list(RecordsIndex().relevant(request, str(root))["solutions"])
    if not solutions:
        return {}
    prepared = briefs(network_of(str(root)), solutions, request)
    return {"briefs": prepared, "considered": dict(NetworkPasses(None).consider(request, prepared, own_passes=own_passes, ask=ask))}


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
        root = slice_root(getattr(cue, "working_dir", None))
        sid = str(getattr(cue, "rid", "") or "")
        ask = ask_for(provider)
        if root is None or not sid or ask is None:
            return ""
        network = network_of(str(root))
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
