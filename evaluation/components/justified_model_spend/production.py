"""Production per-call statistics of a registered prompt, read from the persisted call log (``usage.CallLog``), not from eval traces.

``python3 evaluation/components/justified_model_spend/production.py [--log PATH] [--spec watch-a-run.overseer]`` prints each prompt's
calls and its average and maximum seconds, tokens and dollars per call, as the runner recorded them (``QAR_CALL_LOG_PATH``, default
``qar_daily_usage_calls.jsonl`` beside the usage file). With no such file, or no call of the spec in it, it says so and exits 1: production
latency is never guessed from the eval traces. ``cost_usd`` is 0 when the provider reports no dollars (the claude CLI route)."""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from quest_ai_runner import usage  # noqa: E402


def default_log():
    usage_path = os.environ.get("QAR_DAILY_USAGE_PATH") or "qar_daily_usage.json"
    return os.environ.get("QAR_CALL_LOG_PATH") or str(Path(usage_path).with_suffix("")) + "_calls.jsonl"


def report(log_path, spec_id=None):
    """``(lines, found)``: the printable statistics and whether any call matched."""
    stats = usage.summarize_call_log(log_path, spec_id)
    if not stats:
        return [f"no recorded production call{' of ' + spec_id if spec_id else ''} in {log_path}"], False
    lines = []
    for sid, s in stats.items():
        lines.append(f"{sid}: {s['calls']} calls; seconds avg {s['seconds']['avg']:.2f} max {s['seconds']['max']:.2f}; "
                     f"tokens in avg {s['tokens_in']['avg']:.0f} max {s['tokens_in']['max']:.0f}, out avg {s['tokens_out']['avg']:.0f} "
                     f"max {s['tokens_out']['max']:.0f}; dollars avg {s['cost_usd']['avg']:.6f} max {s['cost_usd']['max']:.6f}")
    return lines, True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", default=None)
    ap.add_argument("--spec", default=None)
    args = ap.parse_args()
    lines, found = report(args.log or default_log(), args.spec)
    print("\n".join(lines))
    sys.exit(0 if found else 1)
