"""Spec selection for the working repository: the cached index, the offline entry point and the four tools."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("esoi.spec_index")
from esoi import spec_index  # noqa: E402

from quest_ai_runner.core import repo_specs, tools_repo_specs  # noqa: E402
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, build_tool_registry  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not hasattr(spec_index, "cached"), reason="the installed esoi predates the cached specification index")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    shutil.copytree(REPO / "esoi", tmp_path / "esoi", ignore=shutil.ignore_patterns("tracker"))
    (tmp_path / "esoi.toml").write_text('package = "x"\nstructure_dir = "esoi"\n', encoding="utf-8")
    monkeypatch.setenv("QAR_SPEC_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("QAR_SPEC_ROOT", str(tmp_path))
    return tmp_path


def test_the_root_is_found_from_the_environment_or_the_working_folder(repo, monkeypatch):
    assert repo_specs.find_root() == repo
    monkeypatch.delenv("QAR_SPEC_ROOT")
    monkeypatch.chdir(repo / "esoi")
    assert repo_specs.find_root() == repo
    monkeypatch.setenv("QAR_SPEC_ROOT", str(repo / "esoi"))   # a folder with no esoi.toml is not a repository
    assert repo_specs.find_root() is None


def test_the_cached_index_is_reused_until_a_record_file_changes(repo):
    first = repo_specs.repo_index(repo)
    cache = next((repo / "cache").glob("*.json"))
    stamp = cache.stat().st_mtime_ns
    assert repo_specs.repo_index(repo)["digest"] == first["digest"] and cache.stat().st_mtime_ns == stamp   # not rebuilt
    problems = repo / "esoi" / "problems.csv"
    problems.write_text(problems.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    repo_specs.repo_index(repo)
    assert cache.stat().st_mtime_ns != stamp                                                                # rebuilt after an edit


def test_find_without_a_model_is_free_and_the_offline_cli_prints_records(repo, capsys):
    got, index = repo_specs.find("a queued task was lost or run twice", repo)
    assert got.calls == 0 and "prob-queued-task-falls-through" in got.ids
    assert repo_specs.main(["find", "a queued task was lost or run twice", "--root", str(repo)]) == 0
    assert "prob-queued-task-falls-through" in capsys.readouterr().out
    assert repo_specs.main(["related", "prob-queued-task-falls-through", "--root", str(repo)]) == 0
    assert repo_specs.main(["show", "no-such", "--root", str(repo)]) == 1


def test_the_four_tools_are_offered_only_in_a_repository_and_read_without_writing(repo, monkeypatch):
    builders = tools_repo_specs.REPO_SPEC_BUILDERS
    specs = {name: build({}) for name, build in builders.items()}
    assert set(specs) == {"find_specs", "related_specs", "intent_specs", "show_spec"} and all(s and not s.mutates for s in specs.values())
    registered = {t["name"] for t in build_tool_registry({"QAR_STANDARD_TOOLS": "1"}).listing()} if hasattr(ToolRegistry, "listing") else set(specs)
    assert set(specs) <= registered
    ctx = ToolContext()
    found = specs["find_specs"].handler({"request": "a queued task was lost or run twice"}, ctx)
    assert found.ok and "prob-queued-task-falls-through" in found.data["ids"]
    related = specs["related_specs"].handler({"id": "prob-queued-task-falls-through", "depth": 2}, ctx)
    assert related.ok and related.data and not specs["related_specs"].handler({"id": "nope"}, ctx).ok
    assert specs["intent_specs"].handler({}, ctx).ok
    assert "prob-queued-task-falls-through" in specs["show_spec"].handler({"id": "prob-queued-task-falls-through", "tier": 2}, ctx).text
    monkeypatch.delenv("QAR_SPEC_ROOT")
    monkeypatch.chdir(repo.parent)
    assert all(build({}) is None for build in builders.values())


def worker_shell(repo, tmp_path, tmp_path_factory, *args):
    """What a deep worker's shell does: a NEW process, started in another folder, with nothing that names the repository."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")   # a sibling of the repository's folder, so no esoi.toml above it
    env = {**os.environ, "QAR_STANDARD_TOOLS": "1", "QAR_SPEC_CACHE": str(tmp_path / "cache")}
    env.pop("QAR_SPEC_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    return subprocess.run([sys.executable, "-m", "quest_ai_runner.tools", *args], cwd=elsewhere, env=env,
                          capture_output=True, text=True, timeout=120)


def test_the_brief_hands_the_worker_the_root_so_the_spec_tools_it_names_exist_in_its_own_shell(repo, tmp_path, tmp_path_factory):
    """A worker told about find_specs called it from its shell, which started elsewhere: "unknown tool 'find_specs'"."""
    request = "request=a queued task was lost or run twice"
    block = build_tool_registry({"QAR_STANDARD_TOOLS": "1"}).render_deep_block("a queued task was lost", pins=("find_specs",))
    assert "- find_specs " in block
    assert f"-m quest_ai_runner.tools --spec-root {shlex.quote(str(repo))} call <name>" in block, "the command carries the root"
    before = worker_shell(repo, tmp_path, tmp_path_factory, "call", "find_specs", "--kw", request)   # the command as it was taught before
    assert before.returncode == 1 and "unknown tool 'find_specs'" in before.stderr + before.stdout
    after = worker_shell(repo, tmp_path, tmp_path_factory, "--spec-root", str(repo), "call", "find_specs", "--kw", request)
    assert after.returncode == 0 and "prob-queued-task-falls-through" in after.stdout


def test_a_brief_names_no_spec_tool_where_there_is_no_repository_to_read_and_adds_no_root_for_other_tools(repo, monkeypatch):
    registry = build_tool_registry({"QAR_STANDARD_TOOLS": "1"})
    assert registry.get("find_specs") is not None and registry.get("find_specs").needs_spec_root
    monkeypatch.delenv("QAR_SPEC_ROOT")
    monkeypatch.chdir(repo.parent)   # the registry was built where records were; the process no longer finds them
    block = registry.render_deep_block("a queued task was lost", pins=("find_specs", "show_spec"))
    assert "find_specs" not in block and "show_spec" not in block and "--spec-root" not in block
    other = build_tool_registry({"QAR_STANDARD_TOOLS": "1"}).get("record_metric_result")
    assert other is not None and not other.needs_spec_root
