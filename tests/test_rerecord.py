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


def test_the_newest_row_of_each_arm_wins_and_free_arms_exclude_model_and_network_arms(tmp_path):
    results = tmp_path / "results.csv"
    write_rows(results, [
        dict(component="a", impl="plain", model_calls="0", cost_usd="0", git_sha="old"),
        dict(component="a", impl="plain", model_calls="0", cost_usd="0", git_sha="new"),
        dict(component="a", impl="live", model_calls="3", cost_usd="0.01", git_sha="new"),
        dict(component="a", impl="net", model_calls="0", cost_usd="0", git_sha="new"),
        dict(component="a", impl="sampled", model_calls="0", cost_usd="0", git_sha="new", sample="sample:3"),
    ])
    latest = rerecord.latest_rows(results)
    assert latest[("a", "plain")]["git_sha"] == "new"
    impls = tmp_path / "a" / "impls"
    impls.mkdir(parents=True)
    (impls / "plain.py").write_text("def run():\n    return 1\n")
    (impls / "live.py").write_text("PAID = True\n")
    (impls / "net.py").write_text("import urllib.request\n")
    (impls / "sampled.py").write_text("x = 1\n")
    assert rerecord.free_arms(latest, tmp_path) == [("a", "plain")]


def test_only_the_items_without_a_current_row_are_left_to_run():
    latest = {("a", "done"): {"git_sha": "x"}, ("a", "stale"): {"git_sha": "y"}}
    reasons = {("a", "done"): "", ("a", "stale"): "stale, code changed since y", ("a", "missing"): "no recorded row"}
    items = [dict(component="a", impl=impl) for impl in ("done", "stale", "missing")]
    out = rerecord.stale_items(items, latest, lambda component, row: reasons[(component, "done" if row is latest[("a", "done")] else
                                                                                "stale" if row is latest[("a", "stale")] else "missing")])
    assert [i["impl"] for i in out if i["why"]] == ["stale", "missing"]
