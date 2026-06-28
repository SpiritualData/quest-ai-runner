"""Tests for the terminal deep-run output UX.

Covers three behaviors of the Textual terminal UI's deep-run display:

1. The calm inline dashboard shows a few legible lines per run (not one), and
   tightens when multiple runs are concurrent.
2. Each deep task's FULL output is persisted into the scrollback transcript when
   it finishes, so it stays readable after the live widgets are hidden.
3. The expanded detail panel holds a run's entire history (no tail truncation)
   and supports paging back/forward with a follow-the-tail toggle.
"""

from __future__ import annotations

import pytest

from quest_ai_runner.interactive import _DeepRunTracker
from quest_ai_runner.textual_ui import QuestAITerminal, DeepDetailPanel


# --- helpers ---------------------------------------------------------------

class _FakeSession:
    """Minimal stand-in so QuestAITerminal can be constructed without a brain."""
    _rep_name = "Tester"
    _console = None


class _RecordingLog:
    """Captures everything written to the transcript as plain text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(getattr(x, "plain", str(x)))


def _make_app() -> tuple[QuestAITerminal, _RecordingLog]:
    app = QuestAITerminal(_FakeSession())
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    log = _RecordingLog()
    app._tlog = log
    return app, log


# --- inline dashboard scaling ---------------------------------------------

def test_dashboard_shows_three_lines_for_a_single_run():
    t = _DeepRunTracker()
    t.add_run("r1", "Goal one")
    for i in range(8):
        t.update_run_output("r1", f"line {i}")
    out = [ln for ln in t.get_dashboard().splitlines() if "line" in ln]
    assert len(out) == 3  # was 1 before; now legible


def test_dashboard_tightens_with_more_concurrent_runs():
    t = _DeepRunTracker()
    for r in ("r1", "r2"):
        t.add_run(r, f"Goal {r}")
        for i in range(5):
            t.update_run_output(r, f"{r}-line {i}")
    per_run = {
        r: len([ln for ln in t.get_dashboard().splitlines() if f"{r}-line" in ln])
        for r in ("r1", "r2")
    }
    assert per_run == {"r1": 2, "r2": 2}  # two runs -> 2 lines each

    t.add_run("r3", "Goal r3")
    for i in range(5):
        t.update_run_output("r3", f"r3-line {i}")
    out3 = [ln for ln in t.get_dashboard().splitlines() if "r3-line" in ln]
    assert len(out3) == 1  # three+ runs -> 1 line each


# --- per-task output persistence ------------------------------------------

def test_finished_deep_task_output_persisted_to_transcript():
    app, log = _make_app()
    app._deep.add_run("r1", "Build the thing")
    app._deep.update_run_output("r1", "reading file [a].py")  # brackets must survive
    app._deep.update_run_output("r1", "wrote patch")
    app._deep.set_run_status("r1", "done")

    app._flush_deep_run("r1")

    body = "\n".join(log.lines)
    assert "⎅ Build the thing" in body
    assert "reading file [a].py" in body  # not misparsed as markup
    assert "wrote patch" in body
    assert "✓ deep task complete" in body


def test_flush_is_idempotent_per_run():
    app, log = _make_app()
    app._deep.add_run("r1", "Goal")
    app._deep.update_run_output("r1", "did a thing")
    app._flush_deep_run("r1")
    n = len(log.lines)
    app._flush_deep_run("r1")  # second call must add nothing
    assert len(log.lines) == n


def test_errored_task_persisted_with_error_marker():
    app, log = _make_app()
    app._deep.add_run("r1", "Goal")
    app._deep.update_run_output("r1", "tried something")
    app._deep.set_run_status("r1", "error")
    app._flush_deep_run("r1")
    assert any("✗ deep task ended with an error" in ln for ln in log.lines)


def test_pending_runs_flushed_at_turn_end():
    """A run that never emitted a terminal phase is still persisted."""
    app, log = _make_app()
    app._deep.add_run("r1", "Unfinished goal")
    app._deep.update_run_output("r1", "some progress")
    # no set_run_status / no terminal phase
    app._flush_pending_deep_runs()
    assert any("some progress" in ln for ln in log.lines)
    assert any("Unfinished goal" in ln for ln in log.lines)


def test_empty_run_writes_no_block_but_is_marked():
    app, log = _make_app()
    app._deep.add_run("r1", "Goal with no output")
    app._flush_deep_run("r1")
    assert log.lines == []          # nothing written
    assert "r1" in app._deep_flushed  # but won't be reconsidered


# --- expanded detail panel: full history + scroll follow -------------------

@pytest.mark.asyncio
async def test_detail_panel_keeps_full_history_and_follow_toggle():
    from textual.app import App, ComposeResult

    class Host(App):
        def compose(self) -> ComposeResult:
            yield DeepDetailPanel(id="deep-detail")

    app = Host()
    async with app.run_test() as pilot:
        panel = app.query_one(DeepDetailPanel)
        history = [f"step {i}" for i in range(200)]
        panel.open_for("r1", "Big goal", history)
        await pilot.pause()

        # Entire history retained (the old tail-view capped at 22).
        assert len(panel._lines) == 200
        assert panel._follow is True

        # Paging back stops following the tail...
        panel.page_back()
        await pilot.pause()
        assert panel._follow is False

        # ...and a new line while paused does not yank us to the bottom.
        panel.push_line("r1", "step 200")
        await pilot.pause()
        assert panel._follow is False
        assert len(panel._lines) == 201
