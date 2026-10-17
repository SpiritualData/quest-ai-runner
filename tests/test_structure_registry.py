"""The evolvable-structure registry stays true: see docs/structure/README.md.

Runs scripts/check_structure.py in-process (no network, no model). A new module that no component
claims, a core module importing an adapter, an oversized module or a stale generated doc fails here.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_structure", ROOT / "scripts" / "check_structure.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repo_structure_has_no_violations():
    problems = _load_checker().check()
    assert problems == [], "\n".join(problems)


def test_glob_star_stays_in_one_folder_and_double_star_crosses():
    glob_re = _load_checker().glob_re
    assert glob_re("a/*.py").match("a/x.py")
    assert not glob_re("a/*.py").match("a/b/x.py")
    assert glob_re("a/**").match("a/b/x.py")


def test_an_unclaimed_file_is_reported(tmp_path, monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "repo_files", lambda: ["quest_ai_runner/core/brand_new_module.py"])
    problems = mod.check()
    assert any(p.startswith("S1 quest_ai_runner/core/brand_new_module.py: belongs to no component") for p in problems)


def test_selected_implementation_beaten_in_results_needs_a_waiver(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "read_csv", lambda p, _r=mod.read_csv: (
        [] if p.name == "waivers.csv" else _r(p)))
    monkeypatch.setattr(mod.leaderboard, "challengers", lambda rows, sel: [{"impl": "rival"}])
    problems = mod.check()
    assert any(p.startswith("S10 route-a-message") and "rival beat" in p for p in problems)


def test_a_component_with_an_unknown_layer_or_no_role_is_reported(monkeypatch):
    mod = _load_checker()
    real = mod.read_csv

    def fake(p):
        rows = real(p)
        if p.name == "components.csv":
            rows[0]["layer"] = "basement"
            rows[1]["role"] = ""
        return rows
    monkeypatch.setattr(mod, "read_csv", fake)
    problems = mod.check()
    assert any(p.startswith("S2 core-ports") and "layer" in p for p in problems)
    assert any(p.startswith("S2 core-orchestrator-loop") and "role is empty" in p for p in problems)


def test_impl_kind_tells_live_code_from_alternatives_and_unclear(tmp_path):
    kind = _load_checker().impl_kind

    def make(name, text):
        p = tmp_path / name
        p.write_text(text)
        return p
    assert kind(make("live.py", '"""CURRENT."""\ndef build(m):\n    from quest_ai_runner.core.tools import ToolContext\n')) == "live"
    assert kind(make("viacommon.py", '"""x"""\nimport common\norch = common.build_offline_orchestrator\n')) == "live"
    assert kind(make("flag.py", '"""x"""\nALTERNATIVE = True\n')) == "alternative"
    assert kind(make("doc.py", '"""BASELINE: a floor."""\n')) == "alternative"
    assert kind(make("unclear.py", '"""CURRENT: reimplements fusion."""\nimport json\n')) == "unclear"


def test_an_unclear_evaluation_implementation_is_reported(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "impl_kind", lambda p: "unclear")
    problems = mod.check()
    assert any(p.startswith("S12 evaluation/components/") and "alternative" in p for p in problems)


def test_selecting_an_alternative_implementation_needs_a_waiver(monkeypatch):
    mod = _load_checker()
    real = mod.read_csv

    def fake(p):
        rows = real(p)
        if p.name == "solutions.csv":
            for r in rows:
                if r["solution_id"] == "select-context":
                    r["selected"] = "evaluation/components/context_selection/impls/vector_bge_small.py"
        return rows
    monkeypatch.setattr(mod, "read_csv", fake)
    assert any(p.startswith("S12 select-context: selected") for p in mod.check())


def test_a_port_row_missing_from_the_selection_table_is_reported(monkeypatch):
    mod = _load_checker()
    table = dict(mod.load_selection_table())
    table.pop("call-a-model")
    monkeypatch.setattr(mod, "load_selection_table", lambda: table)
    assert any(p.startswith("S14 call-a-model: solutions.csv row has port") for p in mod.check())


def test_a_table_implementation_outside_the_row_is_reported(monkeypatch):
    mod = _load_checker()
    table = {k: dict(v, implementations=dict(v["implementations"])) for k, v in mod.load_selection_table().items()}
    table["write-files"]["implementations"]["x"] = "quest_ai_runner.adapters.anthropic_provider:AnthropicProvider"
    monkeypatch.setattr(mod, "load_selection_table", lambda: table)
    assert any(p.startswith("S14 write-files:") and "does not list" in p for p in mod.check())


def edited(mod, name, edit):
    """read_csv that applies ``edit(rows)`` to one registry file."""
    real = mod.read_csv

    def fake(p):
        rows = real(p)
        if p.name == name:
            edit(rows)
        return rows
    return fake


def solution(rows, sid):
    return next(r for r in rows if r["solution_id"] == sid)


def test_a_score_in_a_gap_is_reported(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "read_csv", edited(mod, "solutions.csv", lambda rows: solution(rows, "call-a-model").update(
        gap="multi_provider scores 54/56 and costs $3 per million tokens")))
    problems = [p for p in mod.check() if p.startswith("S22 solutions.csv call-a-model")]
    assert problems and "54/56" in problems[0]
    for text in ("passes 87%", "about $0.11 a run", "on 56 offline cases", "3 of 52 injected"):
        assert mod.NUMBER_IN_PROSE.search(text), text
    for text in ("case RET-019/020 pins it", "Recorded 2026-10-16", "the top three", "S1 to S23"):
        assert not mod.NUMBER_IN_PROSE.search(text), text


def test_an_evaluation_folder_no_solution_names_is_reported(monkeypatch):
    mod = _load_checker()
    def drop_claude_code_ab(rows):
        for r in rows:
            r["command"] = r["command"].replace("; evaluation/components/run.py claude_code_ab --impl idf_hint", "")
    monkeypatch.setattr(mod, "read_csv", edited(mod, "metrics.csv", drop_claude_code_ab))
    assert any(p.startswith("S21 evaluation/components/claude_code_ab") for p in mod.check())


def test_selected_must_equal_the_selection_table_default(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "read_csv", edited(mod, "solutions.csv", lambda rows: solution(rows, "call-a-model").update(
        selected="claude_cli")))
    assert any(p.startswith("S23 call-a-model") and "differs from the selection table default" in p for p in mod.check())


def test_an_id_repeated_inside_a_cell_is_reported(monkeypatch):
    mod = _load_checker()
    def repeat(rows):
        rows[0]["solutions"] = rows[0]["solutions"] + ";" + rows[0]["solutions"].split(";")[0]
    monkeypatch.setattr(mod, "read_csv", edited(mod, "components.csv", repeat))
    assert any(p.startswith("S18 components.csv") and "times" in p for p in mod.check())


def test_an_invariant_solution_is_never_ranked_and_needs_a_reason(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod.leaderboard, "challengers", lambda rows, sel: [{"impl": "rival"}])
    monkeypatch.setattr(mod, "read_csv", edited(mod, "waivers.csv", lambda rows: rows.clear()))
    problems = mod.check()
    assert any(p.startswith("S10 route-a-message") for p in problems)      # an eval solution is ranked
    assert not any(p.startswith("S10 write-files") for p in problems)      # an invariant one is not
    monkeypatch.setattr(mod, "read_csv", edited(mod, "solutions.csv", lambda rows: solution(rows, "write-files").update(
        invariant_reason="")))
    assert any(p.startswith("S11 write-files") and "invariant_reason" in p for p in mod.check())


def test_a_protocol_needs_a_port_cell_not_a_row_of_its_own(monkeypatch):
    """SearchBackend has no row: it is listed in the port cell of search-the-web. Dropping it from there is reported."""
    mod = _load_checker()
    assert not any(p.startswith("S7 SearchBackend") for p in mod.check())
    monkeypatch.setattr(mod, "read_csv", edited(mod, "solutions.csv", lambda rows: solution(rows, "search-the-web").update(
        port="WebResearch")))
    assert any(p.startswith("S7 SearchBackend: port is listed in the `port` cell of no solutions.csv row") for p in mod.check())


def test_an_invariant_solution_without_a_broken_baseline_is_reported(monkeypatch, tmp_path):
    mod = _load_checker()
    assert not any(p.startswith("S25") for p in mod.check())
    empty = tmp_path / "gate_an_autopilot_pass" / "broken"
    empty.mkdir(parents=True)
    (empty / "__init__.py").write_text("")
    monkeypatch.setattr(mod, "EVALUATION", tmp_path)
    problems = mod.check()
    assert any(p.startswith("S25 gate-an-autopilot-pass") and "no baseline module" in p for p in problems)
    assert any(p.startswith("S25 persist-cards") for p in problems)      # a missing broken/ folder counts too


def load_records():
    spec = importlib.util.spec_from_file_location("records_under_test", ROOT / "quest_ai_runner" / "structure" / "records.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_sync():
    spec = importlib.util.spec_from_file_location("sync_esoi_template", ROOT / "scripts" / "sync_esoi_template.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SKILL_TEXT = """# skill

Intent template definitions (each intent can have multiple):
- **Problem**: A difficulty.
    Specify:
        - What is the problem?
        - Who or what type of person has the problem?
        - Why does this problem matter?
        - Some brand new wording about an unknown thing
    What it's not: A task.
    Connects to: **Solutions** that solve it
- **Solution**: A definition.
    Specify:
      - Scoped Purpose: What types of use cases?
    If technology, specify interface details where applicable:
        - Parameters
        - Effects (other than outputs)
- **Metric**: A measure.
    Specify:
        - Name: What is measured?
        -
        - 
Remember: not part of the template.

## Principles
"""


def test_template_parser_is_tolerant_and_keys_are_stable():
    rows, notes = load_sync().parse(SKILL_TEXT)
    keys = {(r["entity"], r["field_key"]): r for r in rows}
    assert keys[("problem", "problem")]["required"] == "yes"
    assert ("problem", "who_has_it") in keys and ("problem", "why_it_matters") in keys
    assert keys[("solution", "scoped_purpose")]["group"] == "specify"
    assert keys[("solution", "parameters")]["group"] == "interface" and keys[("solution", "parameters")]["required"] == "no"
    assert ("solution", "effects") in keys                                 # "Effects (other than outputs)" -> effects
    assert keys[("metric", "name")]["required"] == "yes"
    assert keys[("problem", "what_it_is_not")]["question"] == "A task."
    assert any("empty bullet" in n and n.startswith("metric") for n in notes)          # the unfinished section is reported
    assert any("no entry in BARE_KEYS" in n for n in notes)                           # a new bare question is reported, not fatal
    assert any("implementation: section missing" in n for n in notes)                 # a missing section is reported
    rows2, _ = load_sync().parse(SKILL_TEXT.replace("Who or what type of person has the problem?", "Which people have the problem, who are they?"))
    assert ("problem", "who_has_it") in {(r["entity"], r["field_key"]) for r in rows2}   # refining wording keeps the key


def test_committed_template_matches_the_skill_when_it_is_reachable():
    sync = load_sync()
    skill = sync.skill_path()
    if not skill.exists():
        pytest.skip("the evolvable-structure skill is not reachable here (public CI)")
    rows, _ = sync.parse(skill.read_text())
    assert sync.render_csv(rows) == sync.TEMPLATE_CSV.read_text(), "run scripts/sync_esoi_template.py"


def test_a_stale_template_is_reported_by_the_check(monkeypatch, tmp_path):
    mod = _load_checker()
    skill = tmp_path / "SKILL.md"
    skill.write_text(SKILL_TEXT)
    monkeypatch.setattr(mod.sync_esoi_template, "skill_path", lambda arg=None: skill)
    assert any(p.startswith("S27 docs/structure/esoi_template.csv: stale") for p in mod.check())


def test_every_template_field_is_a_column_of_its_records():
    template = load_records().load_template(ROOT / "docs" / "structure" / "esoi_template.csv")
    records = load_records()
    for entity, (name, _) in records.FILES.items():
        header = (ROOT / "docs" / "structure" / name).read_text().splitlines()[0].split(",")
        assert set(records.fields(template, entity, True)) <= set(header), (entity, name)


def test_a_new_template_field_is_named_by_the_check(monkeypatch, tmp_path):
    mod = _load_checker()
    template = tmp_path / "esoi_template.csv"
    template.write_text(mod.TEMPLATE.read_text() + "problem,mitigation,What limits the harm?,specify,yes\n")
    monkeypatch.setattr(mod, "TEMPLATE", template)
    assert any(p.startswith("S26 R1 problems.csv: missing template column(s) mitigation") for p in mod.check())


def test_an_empty_template_field_and_a_one_sided_link_are_reported(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod.records, "read_csv", edited(mod.records, "problems.csv", lambda rows: rows[0].update(why_hard="", solution_ids="")))
    problems = mod.check()
    assert any(p.startswith("S26 R2 ") and "why_hard" in p for p in problems)
    assert any(p.startswith("S26 R3 ") and "no solution addresses this problem" in p for p in problems)


def test_a_problem_phrased_as_a_task_or_naming_its_solution_is_reported(monkeypatch):
    mod = _load_checker()

    def rewrite(rows):
        rows[0]["problem"] = "Decide per message whether to answer"
        rows[1]["problem"] = "People wait too long, which route-a-message should fix"
    monkeypatch.setattr(mod.records, "read_csv", edited(mod.records, "problems.csv", rewrite))
    problems = [p for p in mod.check() if p.startswith("S26 R4")]
    assert any("reads as a task" in p for p in problems)
    assert any("names its own solution route-a-message" in p for p in problems)


def test_a_waived_solution_is_the_only_one_allowed_unfilled(monkeypatch):
    mod = _load_checker()
    assert not any(p.startswith("S26") for p in mod.check())
    monkeypatch.setattr(mod, "read_csv", edited(mod, "waivers.csv", lambda rows: rows[:] and [
        rows.remove(r) for r in list(rows) if r["rule"] == "S26" and r["path"] == "verify-offline"]))
    assert any(p.startswith("S26 R2 verify-offline") for p in mod.check())


def test_a_number_in_a_record_field_is_reported(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "read_csv", edited(mod, "problems.csv", lambda rows: rows[0].update(
        why_it_matters="It fails on 54/56 of the cases")))
    assert any(p.startswith("S22 problems.csv") and "54/56" in p for p in mod.check())


def test_detailed_records_name_the_overall_item_they_serve(monkeypatch):
    mod = _load_checker()
    monkeypatch.setattr(mod, "read_csv", edited(mod, "problems.csv", lambda rows: rows[0].update(overall_problem_id="QP99")))
    assert any(p.startswith("S26 prob-") and "QP99" in p for p in mod.check())
