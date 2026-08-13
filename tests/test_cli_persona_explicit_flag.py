"""cli.py's `chat` command must tell InteractiveSession whether the caller EXPLICITLY supplied
--rep / QAR_REP_NAME, not just what the final rep_name string ended up being. A naive
`rep_name != "Assistant"` check would wrongly treat `--rep Assistant` as "not explicit" and
trigger auto-persona-resolution the user didn't ask for -- this regression-tests the actual
boolean computed in cli.py's chat branch, which is threaded through as `rep_specified`.
"""
from __future__ import annotations

from quest_ai_runner import cli
from quest_ai_runner.config import RunnerConfig


def _valid_cfg() -> RunnerConfig:
    return RunnerConfig(
        quest_base_url="http://example.invalid",
        quest_api_key="qsk_test",
        retrieval=object(),
        model_provider=object(),
    )


def _run_chat(argv, monkeypatch):
    """Drive cli.main() down the chat path and capture what start_interactive (ANSI fallback,
    forced by making the Textual UI report unavailable) was called with."""
    monkeypatch.setattr(cli, "_config_from_env", _valid_cfg)

    import quest_ai_runner.textual_session as textual_session
    monkeypatch.setattr(textual_session, "is_textual_available", lambda: False)

    calls = []

    def _fake_start_interactive(cfg, **kwargs):
        calls.append(kwargs)

    import quest_ai_runner.interactive as interactive_mod
    monkeypatch.setattr(interactive_mod, "start_interactive", _fake_start_interactive)

    rc = cli.main(argv)
    assert rc == 0
    assert len(calls) == 1
    return calls[0]


def test_no_rep_flag_is_not_specified(monkeypatch):
    kwargs = _run_chat(["chat"], monkeypatch)
    assert kwargs["rep_specified"] is False
    assert kwargs["rep_name"] == "Assistant"


def test_explicit_rep_assistant_counts_as_specified(monkeypatch):
    """The literal trap this flag exists to avoid: --rep Assistant must NOT be read as
    "the user didn't ask for anything" just because it matches the default string."""
    kwargs = _run_chat(["chat", "--rep", "Assistant"], monkeypatch)
    assert kwargs["rep_specified"] is True
    assert kwargs["rep_name"] == "Assistant"


def test_explicit_rep_other_name_counts_as_specified(monkeypatch):
    kwargs = _run_chat(["chat", "--rep", "River"], monkeypatch)
    assert kwargs["rep_specified"] is True
    assert kwargs["rep_name"] == "River"


def test_persona_file_not_given_is_not_specified(monkeypatch):
    kwargs = _run_chat(["chat"], monkeypatch)
    assert kwargs["persona_specified"] is False


def test_persona_file_given_counts_as_specified(monkeypatch, tmp_path):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("Some persona text.")
    kwargs = _run_chat(["chat", "--persona-file", str(persona_path)], monkeypatch)
    assert kwargs["persona_specified"] is True
