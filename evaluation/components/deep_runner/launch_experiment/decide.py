"""The pre-registered arms, the record of one run and the decision rule of the lean-launch experiment (see README.md).

Pure functions over recorded rows. Nothing here runs a model, a process or the network, and nothing here is run by the build: the
experiment is held until the paid-run hold is lifted. The rule is written down BEFORE any run so that no result can move it.
"""
from typing import Dict, Iterable, List, Optional

# One row per run, from the worker's JSON envelope plus the oracle. ``met`` is the ORACLE's verdict (the commit's tests pass in the
# scratch copy), never the worker's own claim; ``claimed_met`` is what the worker's result said.
RECORD_FIELDS = ("case", "arm", "repeat", "met", "claimed_met", "false_done", "turns", "input_tokens", "cache_read_tokens",
                 "cache_creation_tokens", "output_tokens", "wall_seconds", "total_cost_usd")

# The arms, as the DeepLaunchSettings each launches with (core/deep_launch.py). A is today's launch: every field unset.
ARMS: Dict[str, dict] = {
    "A": {},
    "B": {"tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"], "setting_sources": "", "strict_mcp": True,
          "exclude_dynamic_sections": True, "max_budget_usd": 3.0},
    "C": {"tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"], "setting_sources": "", "strict_mcp": True,
          "exclude_dynamic_sections": True, "max_budget_usd": 3.0, "max_turns": 6, "cards_in_brief": True},
    # E (added after the 2026-10-31 safety review, outside the pre-registered A to C): B's lean flags that keep the user settings
    # (the worker's write and delete guard hooks, model overrides) and the web tools, with no spend ceiling. It is the
    # ``lean_guarded`` profile of core/deep_launch.py.
    "E": {"tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch"], "setting_sources": "user",
          "strict_mcp": True, "exclude_dynamic_sections": True},
}

# The rule. An arm replaces the default launch only if BOTH hold.
MET_TOLERANCE_TASKS = 1          # its met count is within one task of A's (met_arm >= met_A - 1)
TOKEN_DROP_REQUIRED = 0.30       # and its tokens per met goal are at least 30 percent lower than A's


def total_tokens(row: dict) -> float:
    return sum(float(row.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens"))


def summarize(rows: Iterable[dict], arm: str) -> dict:
    mine = [r for r in rows if r["arm"] == arm]
    met = sum(1 for r in mine if str(r.get("met")).lower() in ("1", "true", "yes"))
    false_done = sum(1 for r in mine if str(r.get("false_done")).lower() in ("1", "true", "yes"))
    tokens = sum(total_tokens(r) for r in mine)
    cost = sum(float(r.get("total_cost_usd") or 0) for r in mine)
    return {"arm": arm, "runs": len(mine), "met": met, "false_done": false_done, "tokens": tokens,
            "tokens_per_met": (tokens / met) if met else None, "cost_per_met_usd": (cost / met) if met else None,
            "wall_seconds": sum(float(r.get("wall_seconds") or 0) for r in mine)}


def decide(rows: List[dict], arm: str, baseline: str = "A") -> dict:
    """``{"replaces": bool, "why": str, ...}`` for ``arm`` against ``baseline`` on the same cases. An arm with no met run, or a
    baseline with none, never replaces (tokens per met goal is then undefined, and an unmeasured arm is never a pass)."""
    base, other = summarize(rows, baseline), summarize(rows, arm)
    out = {"baseline": base, "arm": other, "replaces": False}
    if not other["runs"] or base["runs"] != other["runs"]:
        out["why"] = "the arms were not run on the same number of runs"
        return out
    within = other["met"] >= base["met"] - MET_TOLERANCE_TASKS
    if base["tokens_per_met"] is None or other["tokens_per_met"] is None:
        out["why"] = "tokens per met goal is undefined: an arm met nothing"
        return out
    drop = 1.0 - other["tokens_per_met"] / base["tokens_per_met"]
    out.update(token_drop=drop, met_within_one_task=within, flag_for_review=other["false_done"] > base["false_done"])
    out["replaces"] = bool(within and drop >= TOKEN_DROP_REQUIRED)
    out["why"] = (f"met {other['met']} vs {base['met']} (within one task: {within}); tokens per met goal fell {drop:.0%} "
                  f"(need {TOKEN_DROP_REQUIRED:.0%})")
    return out
