"""A deep run reports the full model id its alias resolved to, not just the alias."""
import json

from quest_ai_runner.core import goal_runner
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.interactive_session import _model_display
from quest_ai_runner.runner.executor import _model_used_note


def write_session(tmp_path, records):
    path = tmp_path / "abc.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_resolved_model_reads_last_real_assistant_model(tmp_path, monkeypatch):
    path = write_session(tmp_path, [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-sonnet-4-5-20250929"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
    ])
    monkeypatch.setattr(goal_runner, "resolve_session_file", lambda wd, sid: path)
    assert goal_runner.resolved_model_from_session("/wd", "abc") == "claude-sonnet-4-5-20250929"


def test_resolved_model_none_without_session(monkeypatch):
    monkeypatch.setattr(goal_runner, "resolve_session_file", lambda wd, sid: None)
    assert goal_runner.resolved_model_from_session("/wd", "abc") is None


def test_note_names_tier_and_resolved_id():
    d = DeepResult(met=True, model="sonnet", resolved_model="claude-sonnet-4-5-20250929")
    assert _model_used_note([d]) == "\n\n(Completed with model: sonnet (claude-sonnet-4-5-20250929).)"


def test_note_without_resolved_id_is_unchanged():
    assert _model_used_note([DeepResult(met=True, model="sonnet")]) == "\n\n(Completed with model: sonnet.)"


def test_display():
    assert _model_display("sonnet", "claude-sonnet-4-5-20250929") == "sonnet (claude-sonnet-4-5-20250929)"
    assert _model_display("sonnet") == "sonnet"
    assert _model_display("claude-opus-4-1") == "opus (claude-opus-4-1)"
