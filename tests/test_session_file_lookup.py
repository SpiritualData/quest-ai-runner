"""The live deep-run monitor must find the worker's session file under any Claude config base."""
from pathlib import Path

from quest_ai_runner.core import goal_runner


def _session(base: Path, sid: str) -> Path:
    d = base / "projects" / "-some-project"
    d.mkdir(parents=True)
    f = d / f"{sid}.jsonl"
    f.write_text("{}\n")
    return f


def test_session_in_config_dir_found_even_when_working_dir_has_other_sessions(tmp_path, monkeypatch):
    work = tmp_path / "work"
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    _session(work / ".claude", "other-session")  # working dir holds only an unrelated session
    mine = _session(config, "my-session")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HOME", str(home))
    assert goal_runner._find_session_file("my-session", str(work)) == mine
    assert goal_runner.resolve_session_file(str(work), "my-session") == mine


def test_session_in_home_found_when_working_dir_base_exists(tmp_path, monkeypatch):
    work = tmp_path / "work"
    home = tmp_path / "home"
    _session(work / ".claude", "other-session")
    mine = _session(home / ".claude", "my-session")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert goal_runner._find_session_file("my-session", str(work)) == mine


def test_missing_session_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert goal_runner._find_session_file("nope", str(tmp_path)) is None
