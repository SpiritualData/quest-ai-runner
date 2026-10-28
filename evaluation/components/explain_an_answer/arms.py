"""Shared machinery of the arms: a case's trace as a ``TurnTrace``, the real explanation writer on a metered provider, and the
reading of a payload as text."""
import types

import common  # noqa: F401  (puts the repository on the path)
from quest_ai_runner.core.answer_explanation import TurnTrace, build_payload, render_record_for_prompt, render_signals
from quest_ai_runner.core.orchestrator_judges import JudgesMixin

MODEL = "claude-haiku"   # the fast tier (explain_tier default) on the claude CLI backend


def trace_of(case):
    return TurnTrace(**case["trace"])


def writer_for(provider, mixin=JudgesMixin):
    """The real ``write_answer_explanation`` with the model resolution replaced by the arm's provider."""
    host = types.SimpleNamespace(registry=types.SimpleNamespace(resolve_tier=lambda tier: MODEL),
                                 cfg=types.SimpleNamespace(explain_tier="fast"),
                                 get_provider_for_model=lambda model: provider)
    return lambda trace: mixin.write_answer_explanation(host, trace)


def used_lines(used):
    lines = []
    for card in used.get("cards") or []:
        lines.append(f"Background context: {card['title']}")
    for source in used.get("sources") or []:
        lines.append(f"Source: {source['label']} ({source['item_count']} items)")
    for read in used.get("reads") or []:
        lines.append(f"Looked at: {read['kind']} {read['path']}")
    for act in used.get("actions") or []:
        lines.append(f"Action: {act['goal']}: {act['state']}")
    if used.get("web"):
        lines.append("The web was searched.")
    return lines


def text_of(payload):
    """The payload as a reader gets it: prose sections first, then the recorded facts and signals."""
    out = []
    for key in ("understood", "approach", "confidence"):
        if payload.get(key):
            out.append(str(payload[key]))
    for key in ("assumptions", "limitations", "what_would_change"):
        out += [str(x) for x in payload.get(key) or []]
    out += [str(v) for v in (payload.get("evolvable_structure") or {}).values()]
    out += used_lines(payload.get("used") or {})
    signals = payload.get("signals") or {}
    if signals.get("goal_met") is not None:
        out.append("Answer checked against what was asked for: " + ("met" if signals["goal_met"] else "not met")
                   + (f" ({signals['verdict_reason']})" if signals.get("verdict_reason") else ""))
    if signals.get("claims_unexecuted"):
        out.append("Warning: the answer claimed a change the record does not back.")
    for entry in payload.get("metrics") or []:
        out.append(f"Metric {entry.get('name') or entry.get('metric_id')}: before {entry.get('before')}, after {entry.get('after')}")
    if payload.get("metric_missing"):
        out.append("No metric result was recorded for this change.")
    return "\n".join(out)


def prose_of(payload):
    """Only the written half of the payload (what a model composed), for the plain-language checks."""
    return "\n".join(text_of({k: v for k, v in payload.items() if k not in ("used", "signals", "metrics", "metric_missing")}).splitlines())


def grader_record(trace):
    """What the grader may treat as true: the record the writer was constrained to, plus the other facts the trace holds and the
    panel's recorded half shows (the run's exit reason and verdict, the planning steps, the item counts of its sources)."""
    signals = render_signals(trace)
    lines = [render_record_for_prompt(trace), f"Run outcome facts: exit reason {signals['exit_reason'] or 'none'}; goal met {signals['goal_met']}; "
             f"planning steps {signals['steps']}; deep run {signals['deep']}."]
    lines += [f"Source {s.get('label')}: {s.get('item_count', 0)} items." for s in trace.sources if isinstance(s, dict)]
    return "\n".join(lines)


def result_of(trace, payload):
    return {"payload": payload, "text": text_of(payload), "prose": prose_of(payload), "record": grader_record(trace)}


def trace_only(trace):
    return result_of(trace, build_payload(trace, None))
