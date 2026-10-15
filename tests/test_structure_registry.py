"""The adaptable-structure registry stays true: see docs/structure/README.md.

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
    problems = mod.check()
    assert any(p.startswith("S10 route-a-message") for p in problems)


def test_done_component_without_a_metric_kind_is_reported(monkeypatch):
    mod = _load_checker()
    real = mod.read_csv

    def fake(p):
        rows = real(p)
        if p.name == "components.csv":
            rows[0]["metric_kind"] = "none"
        return rows
    monkeypatch.setattr(mod, "read_csv", fake)
    assert any(p.startswith("S11 core-ports") for p in mod.check())
