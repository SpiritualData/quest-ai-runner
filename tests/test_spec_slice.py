"""How QAR points the esoi library's slice source at a repository: ``slice_root`` and the default arms.

The slice, the meters and the ledger are the esoi library's (``esoi.spec_slice``, ``esoi.metric_meter``) and are tested there.
What is QAR's own is where the records are looked for: the goal's folder, never the process's own, with ``QAR_SPEC_ROOT``
naming the repository when it is set, and the index cache ``QAR_SPEC_CACHE`` names. A throwaway repository written by
``esoi init`` stands in for a real one. Offline: no model, no network.
"""
import csv
from pathlib import Path

import pytest
from esoi import scaffold

from quest_ai_runner.core import repo_specs
from quest_ai_runner.core.goal_iteration import SliceAndWriteback, SliceOnly

GREETING = "make the greeting function greet a person by name in the standard form"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scaffolded esoi repository whose one measured solution is greet-a-person (arm ``live``, recorded 2 of 2)."""
    root = tmp_path / "repo"
    scaffold.init(root, "demo_pkg", say=lambda *_: None)
    monkeypatch.setenv("HOME", str(tmp_path))   # the specification index cache lives under the home folder
    monkeypatch.delenv("QAR_SPEC_ROOT", raising=False)
    monkeypatch.delenv("QAR_SPEC_CACHE", raising=False)
    return root


def edit_milestones(root: Path, text: str) -> None:
    path = root / "esoi" / "solutions.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header, rows = reader.fieldnames, list(reader)
    for row in rows:
        if row["solution_id"] == "greet-a-person":
            row["milestones"] = text
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_slice_root_is_the_goals_folder_and_never_a_guess(repo, tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(repo)   # the process's own folder holds records, and still counts for nothing
    assert repo_specs.slice_root(None) is None
    assert repo_specs.slice_root(str(bare)) is None
    assert repo_specs.slice_root(str(repo / "esoi")) == repo.resolve()


def test_qar_spec_root_names_the_repository_even_with_no_folder(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_SPEC_ROOT", str(repo))
    assert repo_specs.slice_root(None) == repo.resolve()
    assert repo_specs.slice_root(str(tmp_path)) == repo.resolve()   # the override wins over where the work happens


def test_the_default_arm_finds_the_slice_of_a_clear_request_and_keeps_its_index_in_the_cache(repo, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("QAR_SPEC_CACHE", str(cache))
    found = SliceOnly().source.slice_for(GREETING, str(repo))
    assert (found.solution_id, found.metric_id, found.milestone_number) == ("greet-a-person", "greeting-pass-rate", 1)
    assert any(cache.glob("*.json"))
    assert SliceOnly().source.slice_for(GREETING, None) is None   # no folder to look from: no guess about where


def test_an_arm_that_measures_reads_with_the_configured_meters(repo):
    arm = SliceAndWriteback()
    assert [m.name for m in arm.meters] == ["recorded_row", "offline_arm"] and arm.measures


def test_an_unclear_or_unrelated_request_gets_no_slice_rather_than_a_wrong_one(repo):
    source = SliceOnly().source
    assert source.slice_for("write a poem about the sea", str(repo)) is None
    assert source.slice_for("", str(repo)) is None


def test_a_met_milestone_is_skipped_to_the_next_open_one(repo):
    edit_milestones(repo, "1. Greetings keep the name: 50% [met: 2026-10-01] 2. Greetings keep the title: 80%.")
    found = SliceOnly().source.slice_for(GREETING, str(repo))
    assert (found.milestone_number, found.target) == (2, 0.8)


def test_a_goals_reading_is_bounded_by_the_configured_arm_time_limit(repo, monkeypatch):
    from quest_ai_runner.core.goal_metrics import GoalMetrics
    from quest_ai_runner.core.runtime_settings import core_settings
    monkeypatch.setenv("QAR_METRIC_ARM_TIMEOUT_SECONDS", "7")
    record = GoalMetrics(SliceAndWriteback(), object(), "run1").record
    assert record.budget_seconds == core_settings().metric_arm_timeout_seconds == 7.0
