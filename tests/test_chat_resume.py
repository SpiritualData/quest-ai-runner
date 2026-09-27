"""`quest-ai-runner chat --resume [ID]` reopens a saved chat conversation.

Covers finding the conversation (most recent for this corpus, id/prefix, ambiguity, missing),
the CLI plumbing (resolved before the TUI starts; a bad id is a plain error), and the session
restoring the turns into the SAME history list the conversation store reads, then writing new
turns back to the same file.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from quest_ai_runner import cli
from quest_ai_runner.chat_conversations import (
    list_conversations, read_conversation, resolve_conversation,
)
from quest_ai_runner.config import RunnerConfig


def write_conv(conv_dir, hexid, turns, corpus_root=None, goal_id=None, age=0.0):
    messages = []
    for user, asst in turns:
        messages.append({"role": "user", "content": user})
        if asst is not None:
            messages.append({"role": "assistant", "content": asst})
    payload = {"messages": messages}
    if corpus_root:
        payload["corpus_root"] = corpus_root
    if goal_id:
        payload["goal_id"] = goal_id
    path = conv_dir / f"qar_chat_{hexid}.json"
    path.write_text(json.dumps(payload))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_read_pairs_turns_and_keeps_unanswered_question(tmp_path):
    path = write_conv(tmp_path, "aa11", [("hi", "hello"), ("still there?", None)])
    conv = read_conversation(path)
    assert conv.history == [("hi", "hello"), ("still there?", "")]
    assert conv.short_id == "aa11"


def test_most_recent_for_this_corpus(tmp_path):
    write_conv(tmp_path, "old1", [("a", "b")], corpus_root="/corpus/x", age=100)
    write_conv(tmp_path, "new2", [("c", "d")], corpus_root="/corpus/other", age=1)
    write_conv(tmp_path, "mid3", [("e", "f")], corpus_root="/corpus/x", age=50)
    assert resolve_conversation(None, tmp_path, corpus_root="/corpus/x").short_id == "mid3"
    assert resolve_conversation("last", tmp_path).short_id == "new2"


def test_legacy_file_without_corpus_is_listed(tmp_path):
    write_conv(tmp_path, "legacy", [("a", "b")])
    assert [c.short_id for c in list_conversations(tmp_path, corpus_root="/corpus/x")] == ["legacy"]


def test_resolve_by_prefix_full_id_and_errors(tmp_path):
    write_conv(tmp_path, "abc123", [("a", "b")])
    write_conv(tmp_path, "abd456", [("c", "d")])
    assert resolve_conversation("abc", tmp_path).short_id == "abc123"
    assert resolve_conversation("qar_chat_abd456", tmp_path).short_id == "abd456"
    assert resolve_conversation("qar_chat_abd456.json", tmp_path).short_id == "abd456"
    with pytest.raises(LookupError, match="matches 2"):
        resolve_conversation("ab", tmp_path)
    with pytest.raises(LookupError, match="no saved chat conversation matches"):
        resolve_conversation("zzz", tmp_path)


def test_nothing_to_resume(tmp_path):
    with pytest.raises(LookupError, match="no saved chat conversations"):
        resolve_conversation(None, tmp_path / "missing")


# -- CLI --------------------------------------------------------------------------------------

def run_chat(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("QAR_CHAT_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_config_from_env", lambda config_path=None: RunnerConfig(
        quest_base_url="http://example.invalid", quest_api_key="qsk_test",
        retrieval=object(), model_provider=object(), corpus_root=None))
    import quest_ai_runner.textual_session as textual_session
    monkeypatch.setattr(textual_session, "is_textual_available", lambda: True)
    calls = []
    monkeypatch.setattr(textual_session, "start_textual_interactive",
                        lambda cfg, **kw: calls.append(kw))
    return cli.main(argv), calls


def test_cli_resume_most_recent(monkeypatch, tmp_path):
    write_conv(tmp_path, "old1", [("a", "b")], age=100)
    write_conv(tmp_path, "new2", [("c", "d")], age=1)
    rc, calls = run_chat(["chat", "--resume"], monkeypatch, tmp_path)
    assert rc == 0
    assert calls[0]["resume"].short_id == "new2"


def test_cli_resume_by_id(monkeypatch, tmp_path):
    write_conv(tmp_path, "old1", [("a", "b")], age=100)
    write_conv(tmp_path, "new2", [("c", "d")], age=1)
    rc, calls = run_chat(["chat", "--resume", "old"], monkeypatch, tmp_path)
    assert rc == 0
    assert calls[0]["resume"].short_id == "old1"


def test_cli_without_resume_starts_fresh(monkeypatch, tmp_path):
    write_conv(tmp_path, "new2", [("c", "d")])
    rc, calls = run_chat(["chat"], monkeypatch, tmp_path)
    assert rc == 0
    assert calls[0]["resume"] is None


def test_cli_bad_id_fails_before_tui(monkeypatch, tmp_path):
    rc, calls = run_chat(["chat", "--resume", "nope"], monkeypatch, tmp_path)
    assert rc == 1
    assert calls == []


def test_cli_list_conversations(monkeypatch, tmp_path, capsys):
    write_conv(tmp_path, "abc123", [("what is the plan", "this")])
    rc, calls = run_chat(["chat", "--list-conversations"], monkeypatch, tmp_path)
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "abc123" in out and "what is the plan" in out


# -- session ----------------------------------------------------------------------------------

def test_session_restores_history_and_appends_to_same_file(monkeypatch, tmp_path):
    from quest_ai_runner import interactive_session as mod

    class FakeOrch:
        class cfg:
            instant_ack = False

    monkeypatch.setenv("QAR_CHAT_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr("quest_ai_runner.config.build_orchestrator", lambda cfg, notify=None: FakeOrch())
    monkeypatch.setattr(mod.InteractiveSession, "_load_session_state", lambda self: None)
    monkeypatch.setattr(mod.InteractiveSession, "_build_model_tiers_menu", lambda self: None)
    monkeypatch.setattr(mod.InteractiveSession, "_try_load_skill_by_name", lambda self, n: None)
    monkeypatch.setattr(mod.InteractiveSession, "_refresh_rep_name_from_skill", lambda self: None)

    path = write_conv(tmp_path, "abc123", [("first", "one"), ("second", "two")], goal_id="goal_x")
    conv = resolve_conversation("abc", tmp_path)
    cfg = RunnerConfig(quest_base_url="", quest_api_key="", corpus_root=None)
    sess = mod.InteractiveSession(cfg, resume=conv)

    assert sess._session_file == path
    assert sess._conv_id == "qar_chat_abc123"
    assert sess._session_history == [("first", "one"), ("second", "two")]
    assert sess._turn_count == 2
    assert sess._goal_id == "goal_x"
    # The anaphora store must see the restored turns (same list object, not a copy).
    assert cfg.conversation_store._history is sess._session_history

    sess._session_history.append(("third", "three"))
    sess._write_session_file()
    assert read_conversation(path).history[-1] == ("third", "three")
    assert len(list(tmp_path.glob("qar_chat_*.json"))) == 1
