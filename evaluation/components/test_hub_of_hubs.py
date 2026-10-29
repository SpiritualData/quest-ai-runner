"""Offline tests of QAR's hub_of_hubs component (the generic scorer's tests live in the hub repository): no model call, no
network. Run: .venv/bin/python3 -m pytest evaluation/components/test_hub_of_hubs.py"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (puts the repository and this folder on the path)
import hub_of_hubs  # noqa: E402,F401  (puts the hub package on the path when it is not installed)
from ai_hub_of_hubs import evaluate, registry  # noqa: E402
from ai_hub_of_hubs.measure import KINDS, check  # noqa: E402

from hub_of_hubs import probes  # noqa: E402
from hub_of_hubs.binding import REPO, binding  # noqa: E402
from hub_of_hubs.broken import vendor_grep  # noqa: E402
from hub_of_hubs.impls import registry_scan  # noqa: E402
from hub_of_hubs.score import score_case  # noqa: E402

CASES = json.loads((Path(__file__).parent / "hub_of_hubs" / "cases.json").read_text())


def test_the_generated_doc_is_current():
    done = subprocess.run([sys.executable, str(REPO / "evaluation/components/hub_of_hubs/render_docs.py"), "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_doc_stays_bounded():
    assert len((REPO / "docs" / "hub-of-hubs.md").read_text().splitlines()) <= 120


def test_every_registry_area_has_every_check_as_a_case():
    have = {(c["args"]["area"], c["kind"]) for c in CASES}
    assert have == {(r["area"], k) for r in registry.read("hubs.csv") for k in KINDS}


def test_bindings_cite_real_solutions_and_paths():
    solutions = {r["solution_id"] for r in registry.read("solutions.csv", REPO / "esoi")}
    assert {s["area"] for s in binding().seats} == {r["area"] for r in registry.read("hubs.csv")}
    for seat in binding().seats:
        for sid in registry.split(seat["seam_solution_id"]):
            assert sid in solutions, seat["area"]
        for path in registry.split(seat["code_paths"]):
            assert (REPO / path).exists(), path


def test_the_hub_rows_for_qar_cite_real_paths():
    for cap in registry.read("hub_capabilities.csv"):
        if cap["consumer"] == "quest-ai-runner":
            assert (REPO / cap["evidence_path"]).exists(), cap["capability_id"]
    for item in registry.read("hub_integrations.csv"):
        if item["consumer"] == "quest-ai-runner":
            assert (REPO / item["path"]).exists(), item["integration"]


def test_a_fake_hub_needs_configuration_and_no_core_edit():
    facts, _, _ = evaluate(binding())
    for area in ("web_search", "model_providers", "tools_mcp", "agent_harness", "context_assembly", "evals", "world", "science"):
        assert check(facts[area], "fake_hub")["holds"], area
    assert not check(facts["voice"], "fake_hub")["holds"]                    # unprobed is not proven
    assert not check(facts["memory"], "fake_hub")["holds"]


def test_the_world_is_one_aggregator_connection_with_no_per_app_entry():
    import ai_hub_of_hubs
    text = (Path(ai_hub_of_hubs.__file__).parent / "examples" / "lane-world-hub.toml").read_text()
    assert text.count("[[mcp_servers]]") == 1 and "token_env" in text
    assert probes.world_hub()[0]


def test_qar_has_no_vendor_branch_in_core_and_names_what_it_hand_wires():
    facts, _, numbers = evaluate(binding())
    assert numbers["vendor_branches"] == 0
    assert {a for a, f in facts.items() if f["reinvented"]} == {"world", "model_providers"}


def test_the_live_implementation_beats_the_baseline():
    def passed(build):
        fn = build(None)
        return sum(1 for c in CASES if score_case(fn, c)["ok"])
    live, floor = passed(registry_scan.build), passed(vendor_grep.build)
    assert live > floor and floor < len(CASES) / 2
