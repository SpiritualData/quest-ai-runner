"""Regression test: startup/bootstrap notices must not be shown twice under the Textual UI.

``InteractiveSession`` used to print every bootstrap/index notice (e.g. "Context index:
computing tfdfidf signatures...") straight to the console via ``_Console.dim`` AND forward it
to the live Textual notice callback (``_startup_notify``). Under the Textual UI, the direct
console write leaks to the real stdout underneath the TUI's alternate screen while the TUI
itself also displays the message via the callback, so the same notice appears twice in the
terminal. Plain (non-Textual) mode has no callback, so the direct console write must remain the
only display path there.
"""

from __future__ import annotations

from quest_ai_runner.interactive_session import _make_startup_notifier


class _FakeConsole:
    def __init__(self) -> None:
        self.dim_calls = []

    def dim(self, s: str) -> None:
        self.dim_calls.append(s)


def test_textual_mode_uses_only_the_live_callback():
    """When a startup_notify callback is wired (Textual UI), the console must stay silent."""
    console = _FakeConsole()
    notices = []
    live_calls = []

    notify = _make_startup_notifier(console, notices, startup_notify=live_calls.append)
    notify("Context index: computing tfdfidf signatures in background. Chat is ready now.")

    assert live_calls == ["Context index: computing tfdfidf signatures in background. Chat is ready now."]
    assert console.dim_calls == []
    assert notices == ["Context index: computing tfdfidf signatures in background. Chat is ready now."]


def test_plain_mode_writes_to_console():
    """Without a live callback (plain ANSI fallback), the console is the only display path."""
    console = _FakeConsole()
    notices = []

    notify = _make_startup_notifier(console, notices, startup_notify=None)
    notify("Context index: building for the first time in background.")

    assert console.dim_calls == ["  Context index: building for the first time in background."]
    assert notices == ["Context index: building for the first time in background."]


def test_multiple_notices_each_shown_once():
    console = _FakeConsole()
    notices = []
    live_calls = []

    notify = _make_startup_notifier(console, notices, startup_notify=live_calls.append)
    notify("Loading context index...")
    notify("Context index: computing tfdfidf signatures in background. Chat is ready now.")

    assert live_calls == [
        "Loading context index...",
        "Context index: computing tfdfidf signatures in background. Chat is ready now.",
    ]
    assert console.dim_calls == []
