"""Internal per-stage bootstrap/scan logs (`quest-ai-runner.context`, `bm25_content_store`) must
not land in the chat transcript by default, in either UI.

Regression: a June fix (commit 6e422ab) raised `quest-ai-runner.context`'s logger to WARNING
inside `InteractiveSession.__init__`, before `build_orchestrator()` spawns the background bootstrap
thread. It was deleted wholesale in August when the ANSI terminal grew a panel-aware log handler
for a DIFFERENT problem (log lines corrupting the spinner's cursor math), leaving the ANSI path
with no level-based suppression at all (root stays at whatever cli.py's `logging.basicConfig()`
set, INFO by default) and Textual's own on_mount-based verbosity handling as the only defense
there. Restored as `_suppress_background_bootstrap_logs()`, called from `InteractiveSession.__init__`
(shared by both UIs) before `build_orchestrator()`, so it holds regardless of which UI/entry point
constructs the session.
"""
from __future__ import annotations

import logging

from quest_ai_runner.interactive import (
    _BACKGROUND_BOOTSTRAP_LOGGER_NAMES,
    _suppress_background_bootstrap_logs,
)


def _reset_loggers():
    for name in _BACKGROUND_BOOTSTRAP_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_default_call_raises_context_logger_to_warning():
    _reset_loggers()
    try:
        logging.getLogger("quest-ai-runner.context").setLevel(logging.NOTSET)
        _suppress_background_bootstrap_logs(verbose=False)
        log = logging.getLogger("quest-ai-runner.context")
        assert log.level == logging.WARNING
        assert not log.isEnabledFor(logging.INFO)
        assert log.isEnabledFor(logging.WARNING)
    finally:
        _reset_loggers()


def test_default_call_also_raises_bm25_content_store_logger():
    _reset_loggers()
    try:
        _suppress_background_bootstrap_logs(verbose=False)
        log = logging.getLogger("quest_ai_runner.adapters.bm25_content_store")
        assert not log.isEnabledFor(logging.INFO)
    finally:
        _reset_loggers()


def test_verbose_true_is_a_noop_leaves_loggers_alone():
    _reset_loggers()
    try:
        _suppress_background_bootstrap_logs(verbose=True)
        for name in _BACKGROUND_BOOTSTRAP_LOGGER_NAMES:
            log = logging.getLogger(name)
            # Untouched: still NOTSET, so INFO is enabled again once root permits it (e.g. -v).
            assert log.level == logging.NOTSET
    finally:
        _reset_loggers()


def test_does_not_lower_an_already_stricter_level():
    """Must only RAISE the level, never fight an explicit DEBUG set some other way."""
    _reset_loggers()
    try:
        log = logging.getLogger("quest-ai-runner.context")
        log.setLevel(logging.ERROR)  # stricter than WARNING
        _suppress_background_bootstrap_logs(verbose=False)
        assert log.level == logging.ERROR  # unchanged, not lowered to WARNING
    finally:
        _reset_loggers()


def test_raises_an_explicit_debug_level_too_matching_original_behavior():
    """DEBUG is also raised to WARNING by a default (non-verbose) call -- only a level STRICTER
    than WARNING (e.g. ERROR, see the test above) survives untouched. This mirrors the original
    June fix's exact condition (`level == NOTSET or level <= INFO`), restored here verbatim."""
    _reset_loggers()
    try:
        log = logging.getLogger("quest-ai-runner.context")
        log.setLevel(logging.DEBUG)
        _suppress_background_bootstrap_logs(verbose=False)
        assert log.level == logging.WARNING
    finally:
        _reset_loggers()


def test_info_records_are_actually_filtered_end_to_end(caplog):
    """Not just the level attribute: an actual INFO call through the real logger is dropped."""
    _reset_loggers()
    try:
        _suppress_background_bootstrap_logs(verbose=False)
        log = logging.getLogger("quest-ai-runner.context")
        with caplog.at_level(logging.DEBUG):
            log.info("context index: stage 2 — analyzing 5 new files for topics")
            log.warning("context index: bootstrap stage 1 returned no areas")
        messages = [r.message for r in caplog.records if r.name == "quest-ai-runner.context"]
        assert not any("stage 2" in m for m in messages)
        assert any("stage 1 returned no areas" in m for m in messages)
    finally:
        _reset_loggers()
