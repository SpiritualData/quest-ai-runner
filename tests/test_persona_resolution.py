"""Auto-persona-resolution: when a corpus's own top-level CLAUDE.md designates a specific named
persona as the intended owner of the work, a session started with no explicit --rep/--persona-file
should pick it up automatically instead of starting generic ("AI: Assistant").

Covers ``_resolve_persona_from_corpus`` and ``_read_persona_file_in_corpus`` directly (both take a
plain object with the handful of attributes they read, so no full ``RunnerConfig`` /
``build_orchestrator`` setup is needed). Domain-free per this repo's hard rule #2: uses only
fictional placeholder names ("River", "Kai"), never anything tied to a real deployment.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from quest_ai_runner.interactive_session import (
    _read_persona_file_in_corpus,
    _resolve_persona_from_corpus,
)


class _FakeProvider:
    """Minimal ModelProvider stand-in: list_models() for ModelRegistry, answer() for the call."""

    def __init__(self, response=None, *, raises=None, delay: float = 0.0):
        self._response = response
        self._raises = raises
        self._delay = delay
        self.calls = []

    def list_models(self):
        return []  # ModelRegistry falls back to DEFAULT_FALLBACK_TOP, still resolves fine

    def answer(self, messages, *, model, system=None, layers=None):
        self.calls.append({"messages": messages, "model": model})
        if self._delay:
            time.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._response


def _cfg(corpus_root=None, provider=None, model_fallback=None):
    return SimpleNamespace(corpus_root=corpus_root, model_provider=provider,
                           model_fallback=model_fallback)


# --- _read_persona_file_in_corpus -------------------------------------------

def test_read_persona_file_reads_content_inside_root(tmp_path):
    (tmp_path / "personas").mkdir()
    persona_path = tmp_path / "personas" / "river.md"
    persona_path.write_text("River's persona instructions.")
    assert _read_persona_file_in_corpus(str(tmp_path), "personas/river.md") == (
        "River's persona instructions."
    )


def test_read_persona_file_rejects_path_escaping_root(tmp_path):
    outside = tmp_path.parent / "outside_secret.md"
    outside.write_text("should never be read")
    assert _read_persona_file_in_corpus(str(tmp_path), "../outside_secret.md") is None


def test_read_persona_file_missing_file_returns_none(tmp_path):
    assert _read_persona_file_in_corpus(str(tmp_path), "does/not/exist.md") is None


# --- _resolve_persona_from_corpus -------------------------------------------

def test_no_corpus_root_skips_resolution_without_llm_call():
    provider = _FakeProvider(response='{"name": null, "persona_file": null}')
    assert _resolve_persona_from_corpus(_cfg(corpus_root=None, provider=provider)) is None
    assert provider.calls == []


def test_no_claude_md_skips_resolution_without_llm_call(tmp_path):
    provider = _FakeProvider(response='{"name": null, "persona_file": null}')
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider)) is None
    assert provider.calls == []


def test_no_model_provider_wired_returns_none(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nSome instructions.")
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider=None)) is None


def test_no_persona_designated_returns_none(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nJust generic project notes, no named owner.")
    provider = _FakeProvider(response='{"name": null, "persona_file": null}')
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider)) is None
    assert len(provider.calls) == 1  # exactly one LLM call


def test_name_only_resolution(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nRiver is the AI representative here.")
    provider = _FakeProvider(response='{"name": "River", "persona_file": null}')
    result = _resolve_persona_from_corpus(_cfg(str(tmp_path), provider))
    assert result == ("River", None)


def test_name_and_persona_file_resolution(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nRiver's full persona is in persona/river.md.")
    (tmp_path / "persona").mkdir()
    (tmp_path / "persona" / "river.md").write_text("River is warm and precise.")
    provider = _FakeProvider(
        response=json.dumps({"name": "River", "persona_file": "persona/river.md"})
    )
    result = _resolve_persona_from_corpus(_cfg(str(tmp_path), provider))
    assert result == ("River", "River is warm and precise.")


def test_persona_file_escaping_root_falls_back_to_name_only(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nRiver is the rep.")
    provider = _FakeProvider(
        response=json.dumps({"name": "River", "persona_file": "../../etc/passwd"})
    )
    result = _resolve_persona_from_corpus(_cfg(str(tmp_path), provider))
    assert result == ("River", None)


def test_markdown_fenced_json_is_parsed(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nKai is the designated rep.")
    provider = _FakeProvider(response='```json\n{"name": "Kai", "persona_file": null}\n```')
    result = _resolve_persona_from_corpus(_cfg(str(tmp_path), provider))
    assert result == ("Kai", None)


def test_malformed_json_returns_none_not_raises(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nSome text.")
    provider = _FakeProvider(response="not json at all")
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider)) is None


def test_provider_error_returns_none_not_raises(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nSome text.")
    provider = _FakeProvider(raises=RuntimeError("provider exploded"))
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider)) is None


def test_slow_provider_call_times_out_and_returns_none(tmp_path, monkeypatch):
    import quest_ai_runner.interactive_session as interactive_mod
    monkeypatch.setattr(interactive_mod, "_PERSONA_RESOLUTION_TIMEOUT_SECONDS", 0.05)
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nSome text.")
    provider = _FakeProvider(response='{"name": "River", "persona_file": null}', delay=1.0)
    assert _resolve_persona_from_corpus(_cfg(str(tmp_path), provider)) is None


def test_notify_callback_fires_before_the_llm_call(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nRiver is the rep.")
    provider = _FakeProvider(response='{"name": "River", "persona_file": null}')
    notices = []
    _resolve_persona_from_corpus(_cfg(str(tmp_path), provider), notify=notices.append)
    assert any("persona" in n.lower() for n in notices)


def test_exactly_one_llm_call_made(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Corpus\nRiver is the rep.")
    provider = _FakeProvider(response='{"name": "River", "persona_file": null}')
    _resolve_persona_from_corpus(_cfg(str(tmp_path), provider))
    assert len(provider.calls) == 1
