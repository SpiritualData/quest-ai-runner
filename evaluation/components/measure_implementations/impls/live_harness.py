"""CURRENT: the live harness. ``leaderboard.rank``, ``challengers``, ``verdict``, ``arm_kind``, ``unverified``, ``comparable``,
``latest_results``, ``selected_impls`` and ``main``; ``common.record_result``, ``common.load_cases`` and
``common.run_component`` pointed (by patching their folder, repository and csv path constants) at a mini
component and synthetic tables in a temporary folder with its own git repository. Zero model calls."""
import contextlib
import csv
import hashlib
import io
import tempfile
from pathlib import Path

import common
import leaderboard
import quest_ai_runner
from measure_implementations.fixtures import mini_component, write_results, write_solutions


def rank(args):
    rows = {r["impl"]: dict(date="d", **r) for r in args["rows"]}
    ranked = [r["impl"] for r in leaderboard.rank(rows)]
    return {"ranked": ranked, "beats": [r["impl"] for r in leaderboard.challengers(rows, args["selected"])],
            "unverified": [i for i, r in rows.items() if leaderboard.unverified(r)]}


def verdict(args):
    rows = {r["impl"]: dict(date="d", **r) for r in args["rows"]}
    label, detail = leaderboard.verdict(rows, args["selected"])
    return {"verdict": label, "text": detail, "arms": {i: leaderboard.arm_kind(r) for i, r in rows.items()}}


def board(args):
    with tempfile.TemporaryDirectory(prefix="qar_board_") as root:
        results, solutions = Path(root) / "results.csv", Path(root) / "solutions.csv"
        write_results(results, args["rows"])
        write_solutions(solutions, args["selected"])
        saved = leaderboard.RESULTS, leaderboard.selected_impls
        leaderboard.RESULTS, leaderboard.selected_impls = results, lambda: saved[1](solutions)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = leaderboard.main(["--strict"] if args.get("strict") else [])
        finally:
            leaderboard.RESULTS, leaderboard.selected_impls = saved
        text = out.getvalue()
    return {"exit": code, "text": text,
            "beats": [ln.split()[1] for ln in text.splitlines() if ln.strip().startswith("!")],
            "incomparable": [ln.split()[1] for ln in text.splitlines() if ln.strip().startswith("?")]}


def record(args):
    with mini_component(args.get("cases_file", "json")) as (root, comps, name):
        out_csv = root / "results.csv"
        saved = common.HERE, common.REPO, common.RESULTS_CSV
        common.HERE, common.REPO, common.RESULTS_CSV = comps, root, out_csv
        try:
            summary = dict(component=name, impl="good", cases=3, passed=3, dev="2/2", heldout="1/1", model_calls=0,
                           tokens_in=0, tokens_out=0, cli_reported_cost_usd=0.0, seconds=0.1)
            if args.get("dirty"):
                (root / quest_ai_runner.__name__ / "live.py").write_text("X = 2\n")
            common.record_result(summary, date="2026-10-16", path=out_csv)
            first = list(csv.DictReader(out_csv.open()))[-1]
            cases_path = comps / name / ("cases.json" if args.get("cases_file", "json") == "json" else "cases.csv")
            matches = first["cases_sha"] == hashlib.sha256(cases_path.read_bytes()).hexdigest()[:8]
            changes = False
            if args.get("edit_cases"):
                cases_path.write_text(cases_path.read_text() + (" " if cases_path.suffix == ".json" else "M9,echo,\n"))
                common.record_result(summary, date="2026-10-17", path=out_csv)
                changes = list(csv.DictReader(out_csv.open()))[-1]["cases_sha"] != first["cases_sha"]
            rows = list(csv.DictReader(out_csv.open()))
        finally:
            common.HERE, common.REPO, common.RESULTS_CSV = saved
    return {"git_sha": bool(first["git_sha"].replace("-dirty", "")), "dirty": first["git_sha"].endswith("-dirty"),
            "cases_sha_matches": matches, "cases_sha_changes": changes, "rows": len(rows)}


def run(args):
    with mini_component() as (root, comps, name):
        saved = common.HERE
        common.HERE = comps
        argv = ["--impl", args["impl"], "--out", str(root / "out.json")] + list(args.get("argv", []))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                summary, _ = common.run_component(name, __import__(f"{name}.score", fromlist=["score_case"]).score_case, argv)
        except Exception as e:  # noqa: BLE001
            return {"cases": 0, "passed": 0, "error": type(e).__name__}
        finally:
            common.HERE = saved
    return {"cases": summary["cases"], "passed": summary["passed"], "error": None, "model_calls": summary["model_calls"]}


def validity(args):  # noqa: ARG001
    good, bad = run({"impl": "good"}), run({"impl": "bad"})
    return {"cases": good["cases"], "good_passed": good["passed"], "bad_passed": bad["passed"]}


KINDS = {"rank": rank, "verdict": verdict, "board": board, "record": record, "run": run, "validity": validity}


def build(meter):  # noqa: ARG001
    def measure(kind, args):
        return KINDS[kind](args)
    return measure
