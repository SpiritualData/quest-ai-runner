"""The evolvable-structure registry stays true: see docs/structure/README.md.

Runs scripts/check_structure.py in-process (no network, no model). A new module that no component
claims, a core module importing an adapter, an oversized module or a stale generated doc fails here.
"""
import importlib.util
from pathlib import Path

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
            r["metric"] = r["metric"].replace("evaluation/components/claude_code_ab", "the hint arm")
            r["metric_command"] = r["metric_command"].replace("; evaluation/components/run.py claude_code_ab --impl warm_claude", "")
    monkeypatch.setattr(mod, "read_csv", edited(mod, "solutions.csv", drop_claude_code_ab))
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
