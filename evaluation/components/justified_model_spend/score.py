"""Score one prompt's justification, weigh it by its spend, and list what is not measured.

Everything is read from the traces ``measure.py`` kept in ``evaluation/traces/justified_model_spend/`` and from the recorded rows of the owner arms;
nothing here calls a model. ``spend_share`` is the metric: the share of AVERAGE dollars per turn, over the measured prompts,
that sits on prompts that pass."""
import importlib
import json
import pkgutil
from pathlib import Path

import common

HERE = Path(__file__).resolve().parent
MEASURED = HERE.parents[1] / "traces" / "justified_model_spend"   # written by measure.py, outside code_paths


def trace(component, impl):
    path = MEASURED / f"{component}__{impl}.json"
    return json.loads(path.read_text()) if path.is_file() else None


def in_scope(case, turn):
    return "*" in case["case_kinds"] or turn["kind"] in case["case_kinds"]


def stats_of(case):
    """Fire rate, tokens, dollars and seconds of the prompt from the keep trace, each average and maximum; None when no trace."""
    kept = trace(case["keep"]["component"], case["keep"]["impl"])
    if kept is None:
        return None
    turns = [t for t in kept["turns"] if in_scope(case, t)]
    ids = {t["id"] for t in turns}
    turn_numbers = {i + 1 for i, t in enumerate(kept["turns"]) if t["id"] in ids}
    calls = [c for c in kept["calls"] if c["spec_id"] == case["spec_id"] and c["turn"] in turn_numbers]
    per_turn = {}
    for c in calls:
        per_turn[c["turn"]] = per_turn.get(c["turn"], 0.0) + c["cost_usd"]

    def avg_max(field):
        values = [c[field] for c in calls] or [0]
        return {"avg": sum(values) / len(values), "max": max(values)}
    return {"turns": len(turns), "calls": len(calls), "turns_fired": len(per_turn),
            "fire_rate": len(per_turn) / len(turns) if turns else 0.0,
            "tokens_in": avg_max("tokens_in"), "tokens_out": avg_max("tokens_out"),
            "cost_per_call": avg_max("cost_usd"), "seconds_per_call": avg_max("seconds"),
            "avg_spend_per_turn": sum(per_turn.values()) / len(turns) if turns else 0.0,
            "max_spend_per_turn": max(per_turn.values()) if per_turn else 0.0,
            "model": (kept.get("summary") or {}).get("model"), "ids": sorted(ids), "label": kept.get("label", ""),
            "right": sum(1 for t in turns if t["ok"])}


def right_without_the_call(case, ids):
    cut = trace(case["ablation"]["component"], case["ablation"]["impl"])
    if cut is None:
        return None
    done = {t["id"]: t["ok"] for t in cut["turns"]}
    if not set(ids) <= set(done):
        return None
    return sum(1 for i in ids if done[i])


def registered(spec_id):
    from quest_ai_runner.core.prompt_specs import SPECS_BY_ID
    load_all_specs()
    return spec_id in SPECS_BY_ID


def load_all_specs():
    import quest_ai_runner
    for module in pkgutil.walk_packages(quest_ai_runner.__path__, "quest_ai_runner."):
        try:
            importlib.import_module(module.name)
        except Exception:  # noqa: BLE001  (an optional dependency missing: its specs are simply not loaded)
            pass


def owner_row_current(case):
    """The reason the owner arm's latest recorded row does not count, or "" when it is a current row."""
    from qar_overall import combine
    comp, impl = case["keep"]["component"], case["keep"]["impl"]
    history = combine.history().get(comp, {}).get(impl, [])
    row = history[-1] if history else None
    why = combine.current(comp, row)
    kept = trace(comp, impl)
    if not why and kept and (row.get("git_sha") or "") != kept.get("git_sha"):
        return f"the trace is from {kept.get('git_sha')}, not the row's commit {row.get('git_sha')}"
    return why


def judge(case, ablate=True, check_budget=True, check_owner=True):
    """The verdict for one case; the flags let the weaker arms skip the checks they do not make."""
    reasons = []
    if not registered(case["spec_id"]):
        reasons.append("not a registered prompt spec")
    s = stats_of(case)
    if s is None:
        return dict(verdict="fail", reasons=reasons + ["no trace of the owner arm"], stats=None, avg_spend=0.0)
    if check_owner:
        why = owner_row_current(case)
        if why:
            reasons.append(f"owner metric row not current: {why}")
    if ablate:
        cut = right_without_the_call(case, s["ids"])
        if cut is None:
            reasons.append("no ablation trace on the same cases")
        elif s["right"] <= cut:
            reasons.append(f"call does not beat its ablation: {s['right']}/{s['turns']} right with it, {cut}/{s['turns']} without")
    if check_budget:
        b = case["budget"]
        if s["max_spend_per_turn"] > b["cost_usd_per_turn"]:
            reasons.append(f"max ${s['max_spend_per_turn']:.5f} per turn over budget ${b['cost_usd_per_turn']}")
        if s["seconds_per_call"]["max"] > b["seconds_per_call"]:
            reasons.append(f"max {s['seconds_per_call']['max']:.1f}s per call over budget {b['seconds_per_call']}s")
        if b.get("tokens") and s["tokens_in"]["max"] + s["tokens_out"]["max"] > b["tokens"]:
            reasons.append("max tokens over budget")
    return dict(verdict="fail" if reasons else "pass", reasons=reasons, stats=s, avg_spend=s["avg_spend_per_turn"])


def score_case(fn, case):
    got = fn(case)
    s = got.get("stats") or {}
    return dict(ok=got["verdict"] == "pass", got=got["verdict"], want="pass", avg_spend=got.get("avg_spend", 0.0),
                note="ok" if got["verdict"] == "pass" else "; ".join(got["reasons"]),
                fire_rate=round(s.get("fire_rate", 0.0), 3))


def unmeasured():
    """Every registered prompt spec that has no case here: listed, never guessed."""
    from quest_ai_runner.core.prompt_specs import SPECS_BY_ID
    load_all_specs()
    have = {c["spec_id"] for c in json.loads((HERE / "cases.json").read_text())}
    return sorted(set(SPECS_BY_ID) - have)


def spend_share(rows):
    """The metric: average dollars per turn on prompts that pass over average dollars per turn on all measured prompts."""
    total = sum(r.get("avg_spend", 0.0) for r in rows)
    return sum(r.get("avg_spend", 0.0) for r in rows if r.get("ok")) / total if total else 0.0


def aggregate(rows):
    have = unmeasured()
    parts = ", ".join(f"{r['id']} {'PASS' if r['ok'] else 'FAIL'} (${r['avg_spend']:.6f}/turn, fires {r.get('fire_rate', 0)})" for r in rows)
    return (f"justified share of average spend over the measured prompts: {spend_share(rows):.4f}; {parts}; "
            f"UNMEASURED ({len(have)} registered prompts with no owner trace): {', '.join(have)}; "
            f"stats are from eval traces, not production turns")
