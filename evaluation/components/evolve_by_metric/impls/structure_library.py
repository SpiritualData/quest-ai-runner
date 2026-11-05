"""CURRENT, live: the esoi library's own leaderboard decides (``leaderboard.challengers``: a rival on the same cases and sample replaces
the selected arm only on a statistically significant gain, tested paired per case when per-case outcomes exist and by Fisher's exact test on
the pass counts otherwise; a solution may add stricter requirements through a ``Policy``), ``leaderboard.best_guess`` makes a first
selection, and the repository's selection is read the way production reads it (``core.resolve.default_name``, for a solution whose
default is a declared implementation, and ``esoi.runtime.snapshot`` against the committed snapshot). Zero model calls."""
import json

import common
from esoi import leaderboard, selection

from quest_ai_runner.the_self.assembler import resolve


def row_of(impl, spec):
    return {"impl": impl, "component": "evolve_by_metric", "passed": str(spec["passed"]), "cases": str(spec["cases"]),
            "cases_sha": spec.get("cases_sha", ""), "sample": spec.get("sample", ""),
            "model_calls": str(spec.get("model_calls", 0)), "tokens_in": str(spec.get("tokens", 0)), "tokens_out": "0",
            "cost_usd": "0.0", "balanced": ""}


def per_case_rows(spec):
    """``{"a": "PPFP"}`` (one letter per case, P pass, F fail) -> the per-case log rows the library reads."""
    return [{"component": "evolve_by_metric", "impl": impl, "cases_sha": rows_sha(spec, impl), "sample": "", "case_id": f"c{i:03d}",
             "ok": "1" if letter == "P" else "0"}
            for impl, letters in spec["per_case"].items() for i, letter in enumerate(letters)]


def rows_sha(spec, impl):
    return spec["rows"][impl].get("cases_sha", "")


def picked(solution_id):
    """What the running system would use by default, or ``None`` when the records name something it cannot resolve."""
    try:
        return resolve.default_name(solution_id)
    except Exception:  # noqa: BLE001  an unresolvable default is exactly what this case reports
        return None


def build(meter):  # noqa: ARG001
    def evolve(kind, args):
        if kind == "challenger":
            rows = {impl: row_of(impl, spec) for impl, spec in args["rows"].items()}
            if args["selected"] not in rows:
                return {"switch_to": None, "status": "unmeasured"}
            policy = leaderboard.Policy(**args["policy"]) if args.get("policy") else None
            per_case = per_case_rows(args) if args.get("per_case") else None
            winners = leaderboard.challengers(rows, args["selected"], common.CONFIG.evaluation, per_case=per_case, policy=policy)
            if not winners:
                return {"switch_to": None, "status": "keep"}
            return {"switch_to": leaderboard.rank({w["impl"]: w for w in winners})[0]["impl"], "status": "switch"}
        if kind == "first_selection":
            rows = {impl: row_of(impl, spec) for impl, spec in args["rows"].items()}
            chosen, why = leaderboard.best_guess(rows, common.CONFIG.evaluation)
            return {"selected": chosen, "basis": "best guess" if "best guess" in why else "tested"}
        if kind == "drift":
            return {"disagreeing": sorted(s for s, name in args["records"].items() if args["snapshot"].get(s) != name)}
        if kind == "repo_drift":
            shipped = json.loads((common.REPO / "quest_ai_runner" / "esoi_selection.json").read_text(encoding="utf-8"))
            fresh = selection.snapshot(common.CONFIG)
            bad = {s for s in set(shipped) | set(fresh) if shipped.get(s) != fresh.get(s)}
            bad |= {s for s, entry in shipped.items() if entry.get("default") in entry.get("modules", {}) and picked(s) != entry["default"]}
            return {"disagreeing": sorted(bad), "checked": len(shipped)}
        raise ValueError(kind)
    return evolve
