"""Offline tests of QAR's hub_of_hubs component (the generic scorer's tests live in the hub repository): no model call, no
network. Run: .venv/bin/python3 -m pytest evaluation/components/test_hub_of_hubs.py"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (puts the repository and this folder on the path)
import hub_of_hubs  # noqa: E402,F401  (puts the hub package on the path when it is not installed)
from hubofhubs import evaluate, registry  # noqa: E402
from hubofhubs.measure import KINDS, check  # noqa: E402

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


def test_every_registry_area_has_every_check_that_applies_to_it_as_a_case():
    have = {(c["args"]["area"], c["kind"]) for c in CASES}
    assert have == {(r["area"], k) for r in registry.read("hubs.csv") for k in KINDS
                    if k not in registry.CONNECTION_KINDS or registry.should_connect(r)}


def test_the_case_file_is_what_the_hub_generates():
    from hubofhubs import cases
    assert CASES == cases.cases(), "regenerate: python -m hubofhubs.cases > evaluation/components/hub_of_hubs/cases.json"


# THE DEFINITION OF QME9 (2026-10-30). The connection checks (by configuration, one seam, selectable, a fake hub) are cases only for the
# research areas whose call (hubs.csv `call`, from each recommendation) is adopt or coexist; a blank call counts as coexist. An area called
# keep, watch or avoid is not a gap: it has no case for those checks, and the scorer's seam and selectable shares are over the same
# areas. Weights and thresholds are unchanged. The registry facts (registered, fresh, no vendor branch, nothing hand-wired) still cover
# every area. The areas that should be connected and have no seam are the real gaps, listed in the generated docs.
def test_the_real_gaps_are_the_adopt_or_coexist_areas_with_no_seam():
    facts, _, numbers = evaluate(binding())
    wanted = {r["area"] for r in registry.read("hubs.csv") if registry.should_connect(r)}
    assert {a for a, f in facts.items() if f["expected"]} == wanted
    assert numbers["to_connect"] == len(wanted)
    assert numbers["unconnected"] == sorted(a for a in wanted if not facts[a]["seam"])
    kept = {a for a in facts if a not in wanted}
    assert kept and not (kept & set(numbers["unconnected"]))
    assert not {c["args"]["area"] for c in CASES if c["kind"] in registry.CONNECTION_KINDS} & kept


# THE BINDING RULE (corrected 2026-10-30). The registry now breaks each seat into sub-solution areas (72, from 12 broad ones), and
# QAR uses only some of them. The binding therefore records which areas QAR uses, and the rule is no longer "the binding equals the
# registry". It is: (1) every bound area exists in the registry, once, so a renamed or removed area cannot leave a stale binding;
# (2) every area that holds one of QAR's wired hub connections or hand-wired integrations is bound, so a connection cannot float
# without a seam; (3) the areas QAR has a probe for are bound (a probe on an unbound area would never run); (4) each bound area
# cites real solutions and real paths; (5) an unbound area reads as not yet connected (no seam, no fake hub), never as passing.
# The earlier rule also demanded `world`, `tools_mcp` and `science`; those broad areas were replaced by their sub-areas, which are
# `integration_aggregators` and `channels`, `mcp_client`, and `science_find_evidence`.
def test_bindings_cite_real_solutions_and_paths():
    solutions = {r["solution_id"] for r in registry.read("solutions.csv", REPO / "esoi")}
    areas = {r["area"] for r in registry.read("hubs.csv")}
    bound = [s["area"] for s in binding().seats]
    assert len(set(bound)) == len(bound) and set(bound) <= areas, sorted(set(bound) - areas)
    for seat in binding().seats:
        for sid in registry.split(seat["seam_solution_id"]):
            assert sid in solutions, seat["area"]
        for path in registry.split(seat["code_paths"]):
            assert (REPO / path).exists(), path


def test_every_area_qar_wires_or_probes_is_bound():
    bound = {s["area"] for s in binding().seats}
    wired = {c["area"] for c in registry.read("hub_capabilities.csv") if c["consumer"] == "quest-ai-runner"}
    hand = {i["area"] for i in registry.read("hub_integrations.csv") if i["consumer"] == "quest-ai-runner"}
    assert (wired | hand | set(probes.PROBES)) <= bound
    assert {"mcp_client", "integration_aggregators", "science_find_evidence"} <= bound


def test_an_unbound_area_reads_as_not_yet_connected():
    facts, _, _ = evaluate(binding())
    unbound = [a for a in facts if a not in {s["area"] for s in binding().seats}]
    assert len(unbound) > 50
    for area in unbound:
        assert not facts[area]["seam"] and not facts[area]["selectable"] and not check(facts[area], "fake_hub")["holds"], area


def test_the_hub_rows_for_qar_cite_real_paths():
    for cap in registry.read("hub_capabilities.csv"):
        if cap["consumer"] == "quest-ai-runner":
            assert (REPO / cap["evidence_path"]).exists(), cap["capability_id"]
    for item in registry.read("hub_integrations.csv"):
        if item["consumer"] == "quest-ai-runner":
            assert (REPO / item["path"]).exists(), item["integration"]


def test_a_fake_hub_needs_configuration_and_no_core_edit():
    facts, _, _ = evaluate(binding())
    for area in ("web_search", "model_providers", "mcp_client", "agent_harness", "context_assembly", "evals", "integration_aggregators",
                 "science_find_evidence"):
        assert check(facts[area], "fake_hub")["holds"], area
    assert not check(facts["voice"], "fake_hub")["holds"]                    # unprobed is not proven
    assert not check(facts["memory"], "fake_hub")["holds"]


def test_the_world_is_one_aggregator_connection_with_no_per_app_entry():
    import hubofhubs
    text = (Path(hubofhubs.__file__).parent / "examples" / "lane-world-hub.toml").read_text()
    assert text.count("[[mcp_servers]]") == 1 and "token_env" in text
    assert probes.world_hub()[0]


def test_qar_has_no_vendor_branch_in_core_and_names_what_it_hand_wires():
    facts, _, numbers = evaluate(binding())
    assert numbers["vendor_branches"] == 0
    assert {a for a, f in facts.items() if f["reinvented"]} == {"integration_aggregators", "model_providers"}


def test_the_live_implementation_beats_the_baseline():
    def passed(build):
        fn = build(None)
        return sum(1 for c in CASES if score_case(fn, c)["ok"])
    live, floor = passed(registry_scan.build), passed(vendor_grep.build)
    assert live > floor and floor < len(CASES) / 2
