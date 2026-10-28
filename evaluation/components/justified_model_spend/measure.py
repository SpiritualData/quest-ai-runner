"""Run an owner arm once under per-call recording, and keep the trace (paid when the arm is paid; run only by decision).

``python3 evaluation/components/justified_model_spend/measure.py <component> <impl> [--only IDS] [--sample N]
[--max-calls N] [--record]`` drives the owner component's own arm through esoi's ``run_component`` (so the row it records
in the results log is the ordinary one), with the call meter replaced by one that also writes every model call into a
``usage.SpendLog`` tagged with the prompt spec id that wrote its prompt (``PromptSpec.render`` sets the tag; a call whose
tag did not survive a worker thread is tagged with the prompt the case kind belongs to and counted as ``defaulted``).
The trace is written to ``evaluation/traces/justified_model_spend/<component>__<impl>.json``: the rows, the per-call records and the run's
summary. The scorer reads these files only; it never runs a model.

Labelled ``eval-trace``: these calls come from eval cases, not production turns."""
import argparse
import functools
import importlib
import json
import sys
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402
from esoi import harness  # noqa: E402
from quest_ai_runner import usage  # noqa: E402

OUT = HERE.parents[1] / "traces" / "justified_model_spend"   # outside code_paths: a new trace does not stale the owner rows it describes


class SpendMeter(harness.CallMeter):
    """The harness call meter that also records each call (tokens, dollars, seconds, spec tag) in ``spend``."""
    spend = None
    kind_spec = {}
    current_kind = ""
    defaulted = 0

    def wrap(self, provider, methods=("plan", "answer")):
        if getattr(provider, "meter_wrapped", False):
            return provider
        meter = self
        for name in methods:
            original = getattr(provider, name, None)
            if original is None:
                continue

            def make(fn):
                @functools.wraps(fn)   # callers inspect the signature for optional keywords
                def recorded(*args, **kwargs):
                    counters = [getattr(provider, n, 0) or 0 for n in ("tokens_in", "tokens_out", "cost_usd")]
                    started = time.monotonic()
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        seconds = time.monotonic() - started
                        t_in, t_out, cost = (max(0, (getattr(provider, n, 0) or 0) - before)
                                             for n, before in zip(("tokens_in", "tokens_out", "cost_usd"), counters))
                        if cost <= 0:
                            price = harness.price_for(common.CONFIG, meter.model)
                            cost = 0.0 if price is None else (t_in * price["input"] + t_out * price["output"]) / 1e6
                        spec = usage.active_spec()
                        fallback = meter.kind_spec.get(meter.current_kind) or meter.kind_spec.get("*")
                        if spec == usage.UNTAGGED and fallback:
                            spec = fallback
                            meter.defaulted += 1
                        meter.spend.record(spec, t_in, t_out, cost, seconds)
                return recorded
            setattr(provider, name, make(original))
        return super().wrap(provider, methods)


def measure(component, impl, argv, kind_spec):
    """Run ``component``/``impl`` with recording; returns the trace dict and writes it to ``evaluation/traces/justified_model_spend/``."""
    score = importlib.import_module(f"{component}.score")
    spend = usage.SpendLog()
    SpendMeter.spend, SpendMeter.kind_spec, SpendMeter.defaulted = spend, kind_spec, 0
    turn_rows = []

    def scorer(fn, case):
        spend.begin_turn()
        SpendMeter.current_kind = case.get("kind", "")
        usage.tag_spec("")
        before = len(spend.records)
        row = score.score_case(fn, case)
        turn_rows.append({"id": case["id"], "kind": case.get("kind", ""), "ok": bool(row.get("ok")), "calls": len(spend.records) - before})
        return row
    lines = []
    with mock.patch.object(harness, "CallMeter", SpendMeter):
        summary, rows = harness.run_component(component, scorer, ["--impl", impl, *argv], common.CONFIG, say=lambda s: (lines.append(s), print(s)))
    trace = {"label": "eval-trace", "component": component, "impl": impl, "summary": summary, "turns": turn_rows,
             "calls": [vars(r) for r in spend.records], "defaulted_tags": SpendMeter.defaulted,
             "git_sha": summary.get("git_sha") or harness.current_sha(common.CONFIG), "cases_sha": harness.cases_digest(component, common.CONFIG.root / common.CONFIG.evaluation_dir)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{component}__{impl}.json").write_text(json.dumps(trace, indent=1, sort_keys=True), encoding="utf-8")
    return trace


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("component")
    ap.add_argument("impl")
    known, rest = ap.parse_known_args()
    kinds = {"assist_a_terminal_session": {"ack": "assist-a-terminal-session.ack", "classify": "assist-a-terminal-session.ask-classify",
                                           "gate": "assist-a-terminal-session.ask-classify", "judge": "assist-a-terminal-session.goal-judge"},
             "spec_selection": {"*": "spec-selection.batch-judge"}, "overseer_signals": {"*": "watch-a-run.overseer"}}
    measure(known.component, known.impl, rest, kinds.get(known.component, {}))
