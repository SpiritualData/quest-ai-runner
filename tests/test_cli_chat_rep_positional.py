"""`qar <name>` (e.g. `quest-ai-runner chat wadona`) is documented as shorthand for `--rep NAME`.
Regression coverage for the positional `rep` argument on the `chat` subcommand: it must set
rep_name/rep_specified the same as --rep, an explicit --rep must still win over it, and when a
<corpus_root>/<name>/CLAUDE.md file exists it is loaded as the persona automatically (same as
--persona-file), without requiring an LLM call.
"""
from __future__ import annotations

from quest_ai_runner import cli
from quest_ai_runner.config import RunnerConfig


def _valid_cfg(corpus_root=None):
    def _make(config_path=None):
        return RunnerConfig(
            quest_base_url="http://example.invalid",
            quest_api_key="qsk_test",
            retrieval=object(),
            model_provider=object(),
            corpus_root=corpus_root,
        )
    return _make


def _run_chat(argv, monkeypatch, corpus_root=None):
    monkeypatch.setattr(cli, "_config_from_env", _valid_cfg(corpus_root))

    import quest_ai_runner.textual_session as textual_session
    monkeypatch.setattr(textual_session, "is_textual_available", lambda: True)

    calls = []

    def _fake_start_textual(cfg, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(textual_session, "start_textual_interactive", _fake_start_textual)

    rc = cli.main(argv)
    assert rc == 0
    assert len(calls) == 1
    return calls[0]


def test_positional_rep_sets_name_and_specified(monkeypatch):
    kwargs = _run_chat(["chat", "wadona"], monkeypatch)
    assert kwargs["rep_specified"] is True
    assert kwargs["rep_name"] == "wadona"


def test_explicit_rep_flag_wins_over_positional(monkeypatch):
    kwargs = _run_chat(["chat", "wadona", "--rep", "River"], monkeypatch)
    assert kwargs["rep_name"] == "River"


def test_no_positional_no_flag_unchanged(monkeypatch):
    kwargs = _run_chat(["chat"], monkeypatch)
    assert kwargs["rep_specified"] is False
    assert kwargs["rep_name"] == "Assistant"


def test_positional_rep_loads_matching_corpus_folder_persona(monkeypatch, tmp_path):
    (tmp_path / "wadona").mkdir()
    (tmp_path / "wadona" / "CLAUDE.md").write_text("Wadona's persona instructions.")
    kwargs = _run_chat(["chat", "wadona"], monkeypatch, corpus_root=str(tmp_path))
    assert kwargs["rep_name"] == "wadona"
    assert kwargs["persona_specified"] is True
    assert kwargs["persona"] == "Wadona's persona instructions."


def test_positional_rep_with_no_matching_folder_runs_without_persona(monkeypatch, tmp_path):
    kwargs = _run_chat(["chat", "nobody"], monkeypatch, corpus_root=str(tmp_path))
    assert kwargs["rep_name"] == "nobody"
    assert kwargs["persona_specified"] is False


def test_explicit_persona_file_wins_over_positional_folder_lookup(monkeypatch, tmp_path):
    (tmp_path / "wadona").mkdir()
    (tmp_path / "wadona" / "CLAUDE.md").write_text("Folder persona (should be ignored).")
    persona_path = tmp_path / "explicit_persona.md"
    persona_path.write_text("Explicit persona text.")
    kwargs = _run_chat(
        ["chat", "wadona", "--persona-file", str(persona_path)], monkeypatch,
        corpus_root=str(tmp_path))
    assert kwargs["persona"] == "Explicit persona text."
