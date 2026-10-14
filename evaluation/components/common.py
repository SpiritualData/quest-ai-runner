"""Shared core for the per-component QAR evaluations. Nothing here knows about a particular solution.

Three jobs:
  * ``build_offline_orchestrator``: a real QAR Orchestrator with NO deep runner and NO Quest server
    (the safety latch: ``cfg.deep_runner = None`` plus an unroutable Quest URL), so the evaluation can
    only ever call the component under test.
  * ``CallMeter``: counts every model call a run makes (provider ``plan``/``answer``), so cost is
    checked against the harness, not guessed. ``--max-calls`` aborts a run that exceeds its budget.
  * ``load_impl`` / ``run_component``: resolve ``--impl module:function`` and drive it over a case file.
    The harness never imports a solution by name, so a new solution is one new function.
"""
import functools
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

OFFLINE_QUEST_URL = "http://quest.invalid"  # never resolves: nothing can reach a Quest server
SCRUB = ("QAR_TOOLS_FILE", "QAR_CONFIG_FILE", "QAR_CONTEXT_PREAMBLE_FILE", "QAR_LINK_POLICY_FILE",
         "QAR_MODEL_FAST", "QAR_MODEL_BALANCED", "QAR_MODEL_QUALITY", "QAR_MODEL_BEST",
         "QAR_OVERSEER_TIER", "QAR_VERIFY_TIER", "QAR_OVERSEER", "QUEST_TEAM_ID")


class BudgetExceeded(RuntimeError):
    pass


class CallMeter:
    """Counts model calls. ``wrap(provider)`` instruments ``plan`` and ``answer`` in place."""

    def __init__(self, max_calls=None):
        self.calls = 0
        self.max_calls = max_calls
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0  # what the claude CLI reports per call (informational on a subscription login)

    def record(self, n=1):
        self.calls += n
        if self.max_calls is not None and self.calls > self.max_calls:
            raise BudgetExceeded(f"model-call budget {self.max_calls} exceeded")

    def wrap(self, provider):
        if getattr(provider, "_meter_wrapped", False):
            return provider
        for name in ("plan", "answer"):
            original = getattr(provider, name, None)
            if original is None:
                continue

            def make(fn):
                @functools.wraps(fn)  # keeps the signature: callers inspect it for optional kwargs
                def counted(*a, **k):
                    self.record()
                    t_in, t_out, cost = (getattr(provider, n, 0) or 0 for n in ("tokens_in", "tokens_out", "cost_usd"))
                    try:
                        return fn(*a, **k)
                    finally:  # the provider's counters are per thread: the delta is this call's
                        self.tokens_in += (getattr(provider, "tokens_in", 0) or 0) - t_in
                        self.tokens_out += (getattr(provider, "tokens_out", 0) or 0) - t_out
                        self.cost_usd += (getattr(provider, "cost_usd", 0) or 0) - cost
                return counted
            setattr(provider, name, make(original))
        provider._meter_wrapped = True
        return provider


def build_offline_orchestrator(meter, registry=None, planner_tier="haiku"):
    """Real Orchestrator, deep runner disabled, no Quest server. Refuses to return if a deep runner is wired."""
    scratch = tempfile.mkdtemp(prefix="qar_comp_")
    os.environ.update(QUEST_BASE_URL=OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval",
                      QAR_STANDARD_TOOLS="1", QAR_CORPUS_ROOT=scratch, QAR_CONVERSATION_SEARCH="false",
                      QAR_MODEL_BACKEND=os.environ.get("QAR_MODEL_BACKEND", "claude_cli"),
                      QAR_PLANNER_TIER=planner_tier, QUEST_AI_RECENT_CONTEXT_DISABLED="1",
                      QUEST_AI_ANTICIPATION="off")
    for k in SCRUB:
        os.environ.pop(k, None)
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator
    cfg = _config_from_env()
    cfg.deep_runner = None  # THE LATCH: explicit None = execution disabled (config.resolve_deep_runner)
    if registry is not None:
        cfg.tool_registry = registry
    orch = build_orchestrator(cfg)
    if getattr(orch, "deep_runner", None):
        raise SystemExit("deep runner is wired; refusing to run")
    meter.wrap(orch.provider)
    for p in getattr(orch, "providers", {}).values() if isinstance(getattr(orch, "providers", None), dict) else []:
        meter.wrap(p)
    return orch


def load_impl(spec, component):
    """``--impl name`` -> ``<component>/impls/name.py``'s ``build(meter)`` returning the function.
    ``module:function`` is also accepted, for a solution that lives outside the repo."""
    if ":" in spec:
        module, _, attr = spec.partition(":")
        return getattr(importlib.import_module(module), attr)
    mod = importlib.import_module(f"{component}.impls.{spec}")
    return mod.build


def run_component(component, fn_scorer, argv):
    import argparse
    ap = argparse.ArgumentParser(description=f"Evaluate the {component} component on its own.")
    ap.add_argument("--impl", default="qar_planner", help="implementation name (impls/<name>.py) or module:build")
    ap.add_argument("--only", default=None, help="comma-separated case ids")
    ap.add_argument("--max-calls", type=int, default=None, help="abort when model calls exceed this")
    ap.add_argument("--out", default=None, help="results json path")
    args = ap.parse_args(argv)
    cases = json.loads((HERE / component / "cases.json").read_text())
    if args.only:
        keep = {c.strip() for c in args.only.split(",")}
        cases = [c for c in cases if c["id"] in keep]
    meter = CallMeter(args.max_calls)
    fn = load_impl(args.impl, component)(meter)
    rows, started = [], time.time()
    for case in cases:
        before, t0 = meter.calls, time.time()
        try:
            row = fn_scorer(fn, case)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            row = dict(ok=False, note=f"{type(e).__name__}: {e}")
        row.update(id=case["id"], area=case.get("area"), calls=meter.calls - before,
                   seconds=round(time.time() - t0, 1))
        rows.append(row)
        print(f"[{row['id']:9}] {'PASS' if row['ok'] else 'FAIL'} calls={row['calls']} "
              f"{row.get('got', '')} | {row.get('note', '')}")
    passed = sum(r["ok"] for r in rows)
    held = [r for r, c in zip(rows, cases) if c.get("heldout")]
    summary = dict(component=component, impl=args.impl, cases=len(rows), passed=passed,
                   dev=f"{sum(r['ok'] for r in rows if r not in held)}/{len(rows) - len(held)}",
                   heldout=f"{sum(r['ok'] for r in held)}/{len(held)}",
                   model_calls=meter.calls, tokens_in=meter.tokens_in, tokens_out=meter.tokens_out,
                   cli_reported_cost_usd=round(meter.cost_usd, 4), seconds=round(time.time() - started, 1))
    print(f"\n{component} / {args.impl}: {passed}/{len(rows)} passed (dev {summary['dev']}, held-out "
          f"{summary['heldout']}), {meter.calls} model calls, {meter.tokens_in} in / {meter.tokens_out} out "
          f"tokens, CLI-reported ${summary['cli_reported_cost_usd']}, {summary['seconds']}s")
    out = Path(args.out or f"/tmp/qar_components/{component}_{args.impl}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(summary=summary, rows=rows), indent=1))
    print(f"results: {out}")
    return summary, rows
