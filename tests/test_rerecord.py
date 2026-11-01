"""scripts/rerecord.py: the rows are the progress, so a killed re-record resumes at the item that was in flight."""
import csv
import importlib.util
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("rerecord", Path(__file__).resolve().parents[1] / "scripts" / "rerecord.py")
rerecord = importlib.util.module_from_spec(SPEC)
sys.modules["rerecord"] = rerecord
SPEC.loader.exec_module(rerecord)

FIELDS = ["date", "component", "impl", "cases", "passed", "dev", "heldout", "model_calls", "tokens_in", "tokens_out", "cost_usd",
          "seconds", "git_sha", "cases_sha", "model", "details", "sample", "balanced"]


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def test_the_newest_row_of_each_arm_wins(tmp_path):
    results = tmp_path / "results.csv"
    write_rows(results, [
        dict(component="a", impl="plain", git_sha="old"),
        dict(component="a", impl="plain", git_sha="new"),
        dict(component="a", impl="other", git_sha="x"),
    ])
    latest = rerecord.latest_rows(results)
    assert latest[("a", "plain")]["git_sha"] == "new" and latest[("a", "other")]["git_sha"] == "x"


def test_only_the_items_without_a_current_row_are_left_to_run():
    latest = {("a", "done"): {"git_sha": "x"}, ("a", "stale"): {"git_sha": "y"}}
    reasons = {("a", "done"): "", ("a", "stale"): "stale, code changed since y", ("a", "missing"): "no recorded row"}
    items = [dict(component="a", impl=impl) for impl in ("done", "stale", "missing")]
    out = rerecord.stale_items(items, latest, lambda component, row: reasons[(component, "done" if row is latest[("a", "done")] else
                                                                                "stale" if row is latest[("a", "stale")] else "missing")])
    assert [i["impl"] for i in out if i["why"]] == ["stale", "missing"]
