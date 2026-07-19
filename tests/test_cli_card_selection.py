"""Offline tests for select_card_ids_for_text's default LLM-selection behavior in cli.py.

Regression: the auto-card-selection wired into `send` (card_ids on assistant tasks) called
select_card_ids_for_text with no `use_llm` argument, and the function defaulted `use_llm=True`.
That silently turned `send` -- an instant, offline command before auto-card-selection existed --
into one that makes a model call by default, adding latency/cost, with any failure in that path
(provider setup or card-store assembly) swallowed by a bare `except Exception` with no trace.

These tests prove: (1) the default no longer builds an LLM provider at all (keyword/IDF-only,
so `send` stays instant and offline unless a caller opts in), and (2) a failure in either the
LLM-provider path or the card-store assemble is logged, not silently swallowed.
"""
from __future__ import annotations

import logging

from quest_ai_runner import cli
from quest_ai_runner.core.adapters import AssembledContext


def test_default_use_llm_is_false_and_skips_provider_setup(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("_config_from_env must not be called when use_llm defaults off")

    monkeypatch.setattr(cli, "_config_from_env", _boom)

    result = cli.select_card_ids_for_text("anything", corpus=str(tmp_path))

    assert isinstance(result, AssembledContext)
    assert result.card_ids == []


def test_explicit_use_llm_true_still_attempts_provider_setup(tmp_path, monkeypatch):
    calls = []

    def _boom():
        calls.append(True)
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(cli, "_config_from_env", _boom)

    result = cli.select_card_ids_for_text("anything", corpus=str(tmp_path), use_llm=True)

    assert calls == [True]
    assert isinstance(result, AssembledContext)
    assert result.card_ids == []


def test_llm_provider_setup_failure_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    def _boom():
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(cli, "_config_from_env", _boom)

    with caplog.at_level(logging.WARNING, logger="quest-ai-runner"):
        cli.select_card_ids_for_text("anything", corpus=str(tmp_path), use_llm=True)

    assert any("card selection" in r.message.lower() for r in caplog.records)


def test_card_store_assemble_failure_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    class _ExplodingStore:
        def __init__(self, *a, **k):
            pass

        def assemble(self, text):
            raise RuntimeError("corrupt card file")

    monkeypatch.setattr("quest_ai_runner.adapters.file_context_store.FileContextStore",
                        _ExplodingStore)

    with caplog.at_level(logging.WARNING, logger="quest-ai-runner"):
        result = cli.select_card_ids_for_text("anything", corpus=str(tmp_path))

    assert isinstance(result, AssembledContext)
    assert result.card_ids == []
    assert any("card selection failed" in r.message.lower() for r in caplog.records)
