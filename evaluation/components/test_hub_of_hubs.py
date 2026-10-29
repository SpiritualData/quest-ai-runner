"""Offline tests for the hub_of_hubs metric: no model call, no network. Run: .venv/bin/python3 -m pytest evaluation/components/test_hub_of_hubs.py"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (puts the repository and this folder on the path)
from hub_of_hubs import probes, registry, scan  # noqa: E402
from hub_of_hubs.impls import registry_scan  # noqa: E402
from hub_of_hubs.broken import vendor_grep  # noqa: E402
from hub_of_hubs.score import score_case  # noqa: E402

KINDS = ("registered", "by_config", "fresh", "seam", "selectable", "core_clean", "fake_hub")


def test_the_generated_doc_is_current():
    done = subprocess.run([sys.executable, str(registry.REPO / "evaluation/components/hub_of_hubs/render_docs.py"), "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_doc_stays_bounded():
    assert len((registry.REPO / "docs" / "hub-of-hubs.md").read_text().splitlines()) <= 120


def test_every_area_has_every_check_as_a_case():
    cases = json.loads((Path(__file__).parent / "hub_of_hubs" / "cases.json").read_text())
    have = {(c["args"]["area"], c["kind"]) for c in cases}
    for row in registry.read("hubs.csv"):
        for kind in KINDS:
            assert (row["area"], kind) in have, f"{row['area']} lacks a {kind} case"


def test_each_area_has_a_dated_finding_with_two_to_four_candidates():
    findings = registry.read("hub_findings.csv")
    for row in registry.read("hubs.csv"):
        mine = [f for f in findings if f["finding_id"] == row["finding_id"]]
        assert 2 <= len(mine) <= 4, row["area"]
        assert registry.days_since(row["last_researched"]) is not None
        assert all(f["url"].startswith("https://") and f["evidence_url"].startswith("https://") for f in mine)


def test_registry_cites_real_paths_and_real_solutions():
    solutions = {r["solution_id"] for r in registry.read("solutions.csv")}
    for row in registry.read("hubs.csv"):
        for seam in registry.split(row["seam_solution_id"]):
            assert seam in solutions, row["area"]
        for path in registry.split(row["code_paths"]):
            assert (registry.REPO / path).exists(), path
    for cap in registry.read("hub_capabilities.csv"):
        assert (registry.REPO / cap["evidence_path"]).exists(), cap["capability_id"]
        assert cap["connect"] in ("config", "code")


def test_freshness_ages_with_the_calendar(monkeypatch):
    facts, caps = registry.all_facts()
    assert all(f["fresh"] for f in facts)
    monkeypatch.setenv("QAR_HUBS_TODAY", "2027-06-01")
    facts, caps = registry.all_facts()
    m = registry.metrics(facts, caps)
    assert not any(f["fresh"] for f in facts)
    assert m["fresh_share"] == 0 and m["days_max"] > registry.STALENESS_DAYS and m["days_mean"] > registry.STALENESS_DAYS
    monkeypatch.setenv("QAR_HUBS_TODAY", "2026-12-27")      # 59 days after the research: inside the bar
    assert all(f["fresh"] for f in registry.all_facts()[0])


def test_metric_arithmetic_on_a_planted_registry():
    facts = [dict(area="a", registered=True, fresh=True, seam=True, selectable=True, vendor_branches=[], days=10),
             dict(area="b", registered=False, fresh=False, seam=False, selectable=False, vendor_branches=["x"], days=110)]
    caps = [dict(connect="config"), dict(connect="code"), dict(connect="code"), dict(connect="code")]
    m = registry.metrics(facts, caps)
    assert (m["coverage"], m["config_share"], m["fresh_share"]) == (0.5, 0.25, 0.5)
    assert (m["days_max"], m["days_mean"]) == (110, 60)
    assert m["code"] == pytest.approx((0.5 + 0.5 + 0.5) / 3)
    assert m["score"] == pytest.approx((0.5 + 0.25 + 0.5 + 0.5) / 4)


def test_scan_counts_branches_not_tables(tmp_path):
    (tmp_path / "branchy.py").write_text('def f(m):\n    if m.startswith("gemini"):\n        return 1\n    if m == "serper":\n        return 2\n')
    (tmp_path / "table.py").write_text('TABLE = {"gemini": "g", "serper": "s"}\n"""docstring about gemini"""\nx = TABLE.get("gemini")\n')
    found = scan.scan_core({"models": ["gemini"], "search": ["serper"], "none": []}, root=tmp_path)
    assert len(found["models"]) == 1 and "branchy.py:2" in found["models"][0]
    assert len(found["search"]) == 1 and found["none"] == []
    assert scan.scan_core({"m": ["gem"]}, root=tmp_path)["m"] == []          # a vendor term inside another word is not a hit


def test_a_fake_hub_needs_configuration_and_no_core_edit():
    for area in ("web_search", "model_providers", "tools_mcp", "agent_harness", "context_assembly", "evals", "world", "science"):
        assert probes.run(area)["holds"], area
    assert not probes.run("voice")["holds"]                                  # unprobed is not proven
    assert not probes.run("memory")["holds"]


def test_the_live_implementation_beats_the_baseline():
    cases = json.loads((Path(__file__).parent / "hub_of_hubs" / "cases.json").read_text())

    def passed(build):
        fn = build(None)
        return sum(1 for c in cases if score_case(fn, c)["ok"])
    live, floor = passed(registry_scan.build), passed(vendor_grep.build)
    assert live > floor
    assert floor < len(cases) / 2
