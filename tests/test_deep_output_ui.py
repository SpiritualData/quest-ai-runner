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

from quest_ai_runner.core.adapters import EVENT_MILESTONE
from quest_ai_runner.core.metric_results import NOT_RECORDED_LINE, render_line
from quest_ai_runner.interactive_session import _DeepRunTracker
from quest_ai_runner.textual_ui import QuestAITerminal, DeepDetailPanel


# --- helpers ---------------------------------------------------------------

class _FakeSession:
    """Minimal stand-in so QuestAITerminal can be constructed without a brain."""
    _rep_name = "Tester"
    _console = None
    _cfg = None
    _goal_id = None
    _model_hint = None


class _RecordingLog:
    """Captures everything written to the transcript as plain text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        # Text objects expose .plain; Markdown (RichMarkdown) exposes .markup (the raw source).
        if hasattr(x, "plain"):
            self.lines.append(x.plain)
        elif hasattr(x, "markup"):
            self.lines.append(x.markup)
        else:
            self.lines.append(str(x))


class _FakeConsole:
    """Minimal console stub that routes markdown() into the recording log."""

    def __init__(self, log: _RecordingLog) -> None:
        self._log = log

    def markdown(self, text: str) -> None:
        # Store the raw markdown source so test assertions can match against it.
        class _MD:
            def __init__(self, t: str) -> None:
                self.markup = t
        self._log.write(_MD(text))

    def rule(self) -> None:
        pass  # _finish_turn draws a divider after every turn; no-op for the recording stub


def _make_app() -> tuple[QuestAITerminal, _RecordingLog]:
    app = QuestAITerminal(_FakeSession())
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    log = _RecordingLog()
    app._tlog = log
    app._console = _FakeConsole(log)
    return app, log


async def _make_app_after_begin_turn(goal_text: str) -> tuple[QuestAITerminal, _RecordingLog]:
    """Like ``_make_app``, but drives a real ``_begin_turn`` first so ``_auto_pass`` is set the
    way it is in a live turn (1, not the pre-turn default of 0). ``_begin_turn`` needs a mounted
    app (it touches real widgets and queries ``#prompt``), so this runs under ``app.run_test()``.
    The real orchestrator worker it kicks off is replaced with a no-op — this harness is only
    exercising the header/turn-counter bookkeeping, not a live run."""
    app = QuestAITerminal(_FakeSession())
    app._run_stream = lambda user_text: None  # skip the real orchestrator worker
    async with app.run_test() as pilot:
        app._begin_turn(goal_text, echo=True, auto=False)
        await pilot.pause()
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    log = _RecordingLog()
    app._tlog = log
    app._console = _FakeConsole(log)
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


def test_dashboard_line_map_hit_tests_each_runs_own_rows():
    # get_dashboard_with_map returns a {row: run_id} map alongside the text so a click on the
    # dashboard can be routed to the specific run whose block that row falls in.
    t = _DeepRunTracker()
    t.add_run("r1", "Goal one")
    t.update_run_output("r1", "r1 output")
    t.add_run("r2", "Goal two")
    t.update_run_output("r2", "r2 output")
    text, line_map = t.get_dashboard_with_map()
    lines = text.splitlines()
    # Every rendered row is covered, and the two runs are sorted into two contiguous blocks.
    assert set(line_map.values()) == {"r1", "r2"}
    assert len(line_map) == len(lines)
    # The row carrying each run's own output line maps to that run, not the other one.
    r1_output_row = next(i for i, ln in enumerate(lines) if "r1 output" in ln)
    r2_output_row = next(i for i, ln in enumerate(lines) if "r2 output" in ln)
    assert line_map[r1_output_row] == "r1"
    assert line_map[r2_output_row] == "r2"
    # And the header rows for each run's own goal map correctly too.
    r1_header_row = next(i for i, ln in enumerate(lines) if "Goal one" in ln)
    r2_header_row = next(i for i, ln in enumerate(lines) if "Goal two" in ln)
    assert line_map[r1_header_row] == "r1"
    assert line_map[r2_header_row] == "r2"


def test_dashboard_marks_the_active_run():
    # Every run's header carries one expand/collapse arrow: "▾" for the run passed as
    # active_run_id (what Alt+D/Tab/a click would act on, i.e. currently "expanded" into the
    # detail panel), "▸" for the others. Exactly one arrow per run, never two.
    t = _DeepRunTracker()
    t.add_run("r1", "Goal one")
    t.add_run("r2", "Goal two")
    text = t.get_dashboard(active_run_id="r2")
    lines = text.splitlines()
    r1_header = next(ln for ln in lines if "Goal one" in ln)
    r2_header = next(ln for ln in lines if "Goal two" in ln)
    assert "▾" in r2_header
    assert "▸" not in r2_header
    assert "▸" in r1_header
    assert "▾" not in r1_header


def test_dashboard_with_map_empty_when_no_runs():
    t = _DeepRunTracker()
    text, line_map = t.get_dashboard_with_map()
    assert text == ""
    assert line_map == {}


class _FakeClickEvent:
    """Minimal stand-in for textual.events.Click: DeepActivity.on_click only reads ``.y`` and
    calls ``.stop()``, so a full Click construction (which needs a real widget/screen) isn't
    needed to exercise the hit-test + routing logic."""

    def __init__(self, y: int) -> None:
        self.y = y
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_click_on_dashboard_row_expands_that_runs_detail():
    # Clicking a specific run's rendered block should open the SAME detail panel Alt+D would open
    # for it, without needing the run to already be "current" -- unlike Alt+D (which always opens
    # the current/most-recent run), a click lets the user pick ANY concurrent run directly.
    app = QuestAITerminal(_FakeSession())
    async with app.run_test() as pilot:
        app._deep.add_run("r1", "Goal one")
        app._deep.update_run_output("r1", "r1 output")
        app._deep.add_run("r2", "Goal two")
        app._deep.update_run_output("r2", "r2 output")
        dashboard, line_map = app._deep.get_dashboard_with_map()
        app._deep_view.show(dashboard, n_runs=2, line_map=line_map)
        await pilot.pause()

        r2_row = next(row for row, rid in line_map.items() if rid == "r2")
        event = _FakeClickEvent(r2_row)
        app._deep_view.on_click(event)
        await pilot.pause()

        assert event.stopped is True  # must not bubble to the App's refocus-prompt handler
        assert app._deep_detail.display is True
        assert app._deep_detail.active_run_id == "r2"

        # Clicking the SAME run's row again closes the panel (toggle, like Alt+D).
        event2 = _FakeClickEvent(r2_row)
        app._deep_view.on_click(event2)
        await pilot.pause()
        assert app._deep_detail.display is False


@pytest.mark.asyncio
async def test_dashboard_arrow_follows_the_detail_panel_not_the_last_streaming_run():
    # The arrow must track whichever run is actually open in the detail panel, not
    # ``_cur_deep_run`` (whichever run most recently streamed output) -- those are different
    # things, and conflating them left the arrow pointing "expanded" the moment a run started
    # streaming, even though nothing was open, and stuck once the panel closed.
    app = QuestAITerminal(_FakeSession())
    async with app.run_test() as pilot:
        app._turn_active = True  # the live panel only exists while the turn runs
        app._deep.add_run("r1", "Goal one")
        app._deep.update_run_output("r1", "r1 output")
        app._cur_deep_run = "r1"  # simulates a live exec event having streamed for r1
        dashboard, line_map = app._deep.get_dashboard_with_map()
        app._deep_view.show(dashboard, n_runs=1, line_map=line_map)
        await pilot.pause()

        # Nothing is open yet: the header must show the collapsed arrow despite r1 being
        # "current" for streaming purposes.
        assert "▸" in app._deep_view._dashboard
        assert "▾" not in app._deep_view._dashboard

        # Clicking the row opens the panel and must flip the arrow immediately, without
        # waiting for the next throttled exec-event redraw.
        row = next(row for row, rid in line_map.items() if rid == "r1")
        app._deep_view.on_click(_FakeClickEvent(row))
        await pilot.pause()
        assert app._deep_detail.active_run_id == "r1"
        assert "▾" in app._deep_view._dashboard
        assert "▸" not in app._deep_view._dashboard

        # Clicking again closes the panel and must flip the arrow back immediately too.
        app._deep_view.on_click(_FakeClickEvent(row))
        await pilot.pause()
        assert app._deep_detail.display is False
        assert "▸" in app._deep_view._dashboard
        assert "▾" not in app._deep_view._dashboard


@pytest.mark.asyncio
async def test_opening_the_details_after_the_turn_does_not_bring_the_live_panel_back():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test() as pilot:
        app._turn_active = False
        app._deep.add_run("r1", "Goal one")
        app._deep.update_run_output("r1", "Read: a.md")
        app._deep.set_metrics("r1", [{"solution_id": "s", "metric_id": "m", "name": "Pass rate", "before": 0.5,
                                      "after": 0.75, "source": "measured", "disagrees": False}])
        app.action_toggle_deep_detail()
        await pilot.pause()
        assert app._deep_detail.display is True
        assert app._deep_view.display is False, "the live panel belongs to the running turn"
        text = app._deep_detail._body.render().plain
        assert text.index("Read: a.md") < text.index("Pass rate: 0.5 -> 0.75 (measured automatically)"), (
            "the results follow the actions, where the panel's follow-the-tail view ends")


@pytest.mark.asyncio
async def test_click_on_trailing_hint_row_is_a_noop():
    # The dashboard's last rendered row is the "[Alt+D/click] expand..." hint, not part of any
    # run's block -- clicking it must not open a (wrong) run, and must leave the click unstopped
    # so it still bubbles to the App's default "refocus the prompt" handler.
    app = QuestAITerminal(_FakeSession())
    async with app.run_test() as pilot:
        app._deep.add_run("r1", "Goal one")
        dashboard, line_map = app._deep.get_dashboard_with_map()
        app._deep_view.show(dashboard, n_runs=1, line_map=line_map)
        await pilot.pause()

        hint_row = max(line_map.keys()) + 1
        event = _FakeClickEvent(hint_row)
        app._deep_view.on_click(event)
        await pilot.pause()

        assert event.stopped is False
        assert app._deep_detail.display is False


# --- per-task output persistence ------------------------------------------

@pytest.mark.asyncio
async def test_finished_deep_task_output_persisted_to_transcript():
    app, log = await _make_app_after_begin_turn("Build the thing")
    app._deep.add_run("r1", "Build the thing")
    app._deep.update_run_output("r1", "reading file [a].py")  # brackets must survive
    app._deep.update_run_output("r1", "wrote patch")
    app._deep.set_run_status("r1", "done")

    app._flush_deep_run("r1")

    body = "\n".join(log.lines)
    assert "Build the thing" not in body, "one run's record does not repeat the person's own request"
    assert "[Pass" not in body
    assert "reading file [a].py" in body  # not misparsed as markup
    assert "wrote patch" in body
    assert "Deep task complete" not in body, "how the turn ended is said once, by the closing line"


def test_flush_is_idempotent_per_run():
    app, log = _make_app()
    app._deep.add_run("r1", "Goal")
    app._deep.update_run_output("r1", "did a thing")
    app._flush_deep_run("r1")
    n = len(log.lines)
    app._flush_deep_run("r1")  # second call must add nothing
    assert len(log.lines) == n


def test_an_errored_run_is_recorded_without_a_completion_or_error_line():
    """A run's record cannot know how the turn ended (met, stopped, paused, waiting), so it never
    carries a "complete" line, and an errored run never gets a green one."""
    app, log = _make_app()
    app._deep.add_run("r1", "Goal")
    app._deep.update_run_output("r1", "tried something")
    app._deep.set_run_status("r1", "error")
    app._flush_deep_run("r1")
    body = "\n".join(log.lines)
    assert "tried something" in body
    assert "complete" not in body.lower() and "✓" not in body and "✗" not in body


def test_a_heartbeat_is_live_only_and_never_part_of_the_record():
    """The worker's sign of life shows on the live dashboard, not in the permanent record."""
    app, log = _make_app()
    app._deep.add_run("r1", "Goal")
    app._deep.update_run_output("r1", "Read: /a.py")
    app._deep.update_run_output("r1", "Deep run active, 90s elapsed, 12 actions so far", record=False)
    app._deep.update_run_output("r1", "Wrote 14 rows.")
    assert "Deep run active" in app._deep.get_dashboard()
    app._flush_deep_run("r1")
    assert "Deep run active" not in "\n".join(log.lines)
    assert "Wrote 14 rows." in "\n".join(log.lines)


def test_pending_runs_flushed_at_turn_end():
    """A run that never emitted a terminal phase is still persisted."""
    app, log = _make_app()
    app._deep.add_run("r1", "Unfinished goal")
    app._deep.update_run_output("r1", "some progress")
    # no set_run_status / no terminal phase
    app._flush_pending_deep_runs()
    assert any("some progress" in ln for ln in log.lines)
    # One run: its goal is the person's own request, so the record does not repeat it.
    assert not any("Unfinished goal" in ln for ln in log.lines)


def test_empty_run_writes_no_block_but_is_marked():
    app, log = _make_app()
    app._deep.add_run("r1", "Goal with no output")
    app._flush_deep_run("r1")
    assert log.lines == []          # nothing written
    assert "r1" in app._deep_flushed  # but won't be reconsidered


@pytest.mark.asyncio
async def test_final_output_rendered_in_record():
    """The worker's final result is shown as the AI's reply, not the per-op trace."""
    app, log = await _make_app_after_begin_turn("Fix the bug")
    app._deep.add_run("r1", "Fix the bug")
    app._deep.update_run_output("r1", "Read: /a/b.py")
    app._deep.set_final_output("r1", "Patched the off-by-one in foo().\nCommitted as abc123.")
    app._deep.set_run_status("r1", "done")
    app._flush_deep_run("r1")

    body = "\n".join(log.lines)
    assert "Fix the bug" not in body        # one run: its goal is the person's own request, not repeated
    assert "1 file read" in body            # rolled up, not the path
    assert "/a/b.py" not in body            # individual file ops are NOT replayed
    assert "Tester (AI):" in body           # the result reads as the AI's reply, from its speaker
    assert "Result" not in body
    assert "Patched the off-by-one in foo()." in body
    assert "Committed as abc123." in body


def test_activity_summary_rolls_up_tool_actions():
    """Many tool ops collapse into one counts line, not a wall of read/write lines."""
    app, log = _make_app()
    app._deep.add_run("r1", "Do the work")
    for p in ("Read: /a.py", "Read: /b.py", "Read: /c.py", "Edit: /a.py",
              "$ pytest -q", "Using Agent", "I weighed the options."):
        app._deep.update_run_output("r1", p)
    app._deep.set_final_output("r1", "All done.")
    app._flush_deep_run("r1")

    body = "\n".join(log.lines)
    assert "3 files read" in body
    assert "1 edit" in body
    assert "1 command" in body
    assert "1 tool call" in body
    # No individual file paths and no narration when a result exists — that's the wall we cut.
    assert "/a.py" not in body
    assert "I weighed the options." not in body
    assert "All done." in body


def test_finished_run_archived_for_later_replay():
    """A finished run with actions is kept in the cross-turn archive (with its full trace)."""
    app, log = _make_app()
    app._deep.add_run("r1", "Do work")
    app._deep.update_run_output("r1", "Read: /a.py")
    app._deep.update_run_output("r1", "I changed the parser.")
    app._deep.set_run_status("r1", "done")
    app._flush_deep_run("r1")

    assert "r1" in app._deep_archive
    assert app._deep_archive["r1"]["exec_lines"]  # full per-action trace retained


def test_detail_available_after_tracker_reset():
    """Alt+D can still find a run after the next turn rebuilds the live tracker."""
    app, log = _make_app()
    app._deep.add_run("r1", "Do work")
    app._deep.update_run_output("r1", "Read: /a.py")
    app._flush_deep_run("r1")

    # Simulate the next turn rebuilding the live tracker (as _begin_turn does).
    app._deep = _DeepRunTracker()

    runs = app._available_deep_runs()
    assert "r1" in runs               # falls back to the archive
    assert runs["r1"]["exec_lines"]   # with the full trace to replay


def test_result_only_run_not_archived():
    """A run with a result but no captured actions has nothing to replay, so it isn't archived."""
    app, log = _make_app()
    app._deep.add_run("r1", "Answer")
    app._deep.set_final_output("r1", "42.")
    app._flush_deep_run("r1")
    assert "r1" not in app._deep_archive


def test_a_record_never_carries_the_actions_hint_the_closing_line_does():
    """The Alt+D hint is part of the turn's one closing line (tests/test_terminal_run_outcome.py)."""
    app, log = _make_app()
    app._deep.add_run("r1", "Do work")
    app._deep.update_run_output("r1", "Read: /a.py")
    app._flush_deep_run("r1")
    assert not any("Alt+D" in ln for ln in log.lines)
    assert "r1" in app._deep_archive          # but the actions are kept for Alt+D to replay


def test_narration_shown_when_no_result():
    """An errored/incomplete run with no result still shows the worker's own words."""
    app, log = _make_app()
    app._deep.add_run("r1", "Try the thing")
    app._deep.update_run_output("r1", "Read: /x.py")        # counted, not shown
    app._deep.update_run_output("r1", "I think the issue is in the parser.")
    app._deep.set_run_status("r1", "error")
    app._flush_deep_run("r1")

    body = "\n".join(log.lines)
    assert "1 file read" in body
    assert "I think the issue is in the parser." in body
    assert "✗" not in body


def test_final_output_alone_is_enough_to_flush():
    """A run that produced a result but had no captured steps still leaves a record."""
    app, log = _make_app()
    app._deep.add_run("r1", "Answer the question")
    app._deep.set_final_output("r1", "The answer is 42.")
    app._flush_deep_run("r1")
    assert any("The answer is 42." in ln for ln in log.lines)


# --- duplicate-answer regression (2026-07-26 bug report) -------------------

class _FakeDeepFinal:
    """Minimal OrchestratorResult-like stand-in for a completed deep turn. ``text`` mirrors what
    ``Orchestrator._run_deep`` actually returns: never set for a "deep" kind result."""
    kind = "deep"
    text = None
    deep_results = [type("R", (), {"met": True, "output": "did the thing", "tokens": 100})()]
    goals = ["Build the thing"]
    tokens_in = 10
    tokens_out = 20
    model = "claude-sonnet-4-6"
    steps = 1


class _FakeSessionForFinishTurn:
    """A session stand-in with just enough surface for _finish_turn to run."""
    _rep_name = "Tester"
    _cfg = type("Cfg", (), {"corpus_root": None})()
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self._orch = type("Orch", (), {"context_assembler": None})()

    def _write_session_file(self) -> None:
        pass

    def _maybe_refresh_next_steps(self, final) -> None:
        """No-op stand-in for the real session's next-steps write-back."""


@pytest.mark.asyncio
async def test_deep_turn_answer_not_duplicated_after_flush():
    """A deep turn's result gets ONE full record in scrollback (via
    _flush_pending_deep_runs -> _flush_deep_run), not a second copy re-printed as a generic
    "{rep} (AI): Executing: ..." chat bubble underneath it.

    Root cause: the orchestrator's terminal EVENT_RESULT for a "deep" kind result carries the
    same full text the per-run flush already wrote (deep_results' concatenated output), and
    since a deep run never streams via EVENT_PARTIAL, that text lands in _answer_parts just
    like a normal shallow answer would. _finish_turn then printed _answer_parts as a second,
    duplicate bubble underneath the already-flushed record (2026-07-26 live bug report: the
    full "What I changed" summary appeared twice in the terminal, once per code path)."""
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log
        app._console = _FakeConsole(log)
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()

        goal = "Build the thing"
        output = "What I changed:\n\n- did the thing"
        app._deep.add_run("r1", goal)
        app._deep.set_final_output("r1", output)
        app._deep.set_run_status("r1", "done")
        # Mirrors what the live event handlers populate for a deep turn: the terminal
        # EVENT_RESULT's full deep-output text. (The "Executing: {goal}" header that used to land
        # here too is EVENT_INTENT now and deliberately never enters _answer_parts -- see
        # tests/test_no_deep_executor_honesty.py. It is kept in this fixture anyway: the
        # anti-duplication rule this test pins must hold for whatever _answer_parts contains.)
        app._answer_parts = [f"Executing: {goal}", output]
        app._auto_pass = 1

        app._finish_turn("build the thing", _FakeDeepFinal(), 3.4, cancelled=False, error=None)

        body = "\n".join(log.lines)
        assert body.count(output) == 1  # the result body appears exactly once, not twice
        assert "[Pass" not in body
        assert body.count("✓") == 1 and "✓ Done in 3s" in body   # one closing line, not three "done" lines
        assert "Deep task complete" not in body
        assert body.count(f"{app.rep_name} (AI):") == 1  # the result's own label, no second bubble


# --- the metric before and after, written by code, shown once --------------

MEASURED = {"solution_id": "greet-a-person", "metric_id": "greeting-pass-rate", "name": "Greeting pass rate",
            "milestone": "Share of names greeted", "before": 0.5, "after": 0.75, "delta": 0.25, "source": "measured",
            "reason": "measured by offline_arm", "disagrees": True}
RECORDED = {**MEASURED, "after": 0.9, "delta": 0.4, "source": "recorded", "reason": ""}
NOT_APPLICABLE = {"solution_id": "", "metric_id": "", "name": "", "milestone": "", "before": None, "after": None,
                  "delta": None, "source": "not_applicable", "reason": "Only a draft was edited.", "disagrees": False}
# The measured entry and the AI's own record of it are one line, the AI's numbers said neutrally.
PAIR_LINE = "Greeting pass rate: 0.5 -> 0.75 (measured automatically). The AI reported 0.5 -> 0.9."
MEASURED_LINE = "Greeting pass rate: 0.5 -> 0.75 (measured automatically, and the AI reported different numbers)"


def milestone(run_id: str, **data) -> dict:
    return {"type": EVENT_MILESTONE, "text": "Completed: Improve the greeting",
            "data": {"run_id": run_id, "goal": "Improve the greeting", **data}}


def stripped(log: _RecordingLog) -> list[str]:
    return [ln.strip() for ln in log.lines]


def test_a_runs_record_shows_the_metric_before_and_after_once_and_drops_the_plain_text_line():
    app, log = _make_app()
    app._deep.add_run("r1", "Improve the greeting")
    app._deep.update_run_output("r1", "Edit: greet.py")
    entries = [MEASURED, RECORDED]
    # The library ends the result's text with its one plain line for consumers that show text only.
    app._handle_event(milestone("r1", deep_output=f"Changed greet().\n\n{render_line(entries)}", metrics=entries))
    app._deep.set_run_status("r1", "done")
    app._flush_deep_run("r1")

    lines = stripped(log)
    assert lines.count(PAIR_LINE) == 1
    assert not any("Metric:" in ln for ln in lines), "the plain text line is not printed a second time"
    body = "\n".join(log.lines)
    assert "Changed greet()." in body, "the rest of the result is untouched"
    assert body.index(PAIR_LINE) < body.index("Changed greet()"), "the numbers come before the result and its link"


def test_a_change_with_no_metric_recorded_says_so_in_one_dim_line():
    app, log = _make_app()
    app._deep.add_run("r1", "Improve the greeting")
    app._handle_event(milestone("r1", deep_output=f"Changed greet().\n\n{NOT_RECORDED_LINE}", metrics=[],
                                metric_missing=True))
    app._deep.set_run_status("r1", "done")
    app._flush_deep_run("r1")
    assert stripped(log).count(NOT_RECORDED_LINE) == 1
    assert "".join(log.lines).count("No metric") == 1


def test_a_declared_not_applicable_reads_as_such():
    app, log = _make_app()
    app._deep.add_run("r1", "Draft the note")
    app._handle_event(milestone("r1", deep_output="Drafted.", metrics=[NOT_APPLICABLE]))
    app._flush_deep_run("r1")
    assert "Nothing to measure: Only a draft was edited." in stripped(log)


def test_a_milestone_that_arrives_after_the_record_adds_its_lines_after_it():
    app, log = _make_app()
    app._deep.add_run("r1", "Improve the greeting")
    app._deep.set_final_output("r1", "Changed greet().")
    app._flush_deep_run("r1")
    assert MEASURED_LINE not in stripped(log)
    app._handle_event(milestone("r1", metrics=[MEASURED]))
    assert stripped(log).count(MEASURED_LINE) == 1
    app._flush_deep_run("r1")   # a second flush adds nothing
    assert stripped(log).count(MEASURED_LINE) == 1


class _FakeMetricFinal(_FakeDeepFinal):
    """A finished deep turn whose one goal carries its metric record, as ``OrchestratorResult`` does."""
    deep_results = [type("R", (), {"met": True, "output": "Changed greet().", "tokens": 100,
                                   "metric_results": [MEASURED, RECORDED], "metric_missing": False})()]
    metric_results = [MEASURED, RECORDED]
    metric_missing = False
    explanation = None


@pytest.mark.asyncio
async def test_the_turn_shows_each_metric_line_once_from_milestone_to_finish():
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log
        app._console = _FakeConsole(log)
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()
        entries = [MEASURED, RECORDED]
        app._deep.add_run("r1", "Improve the greeting")
        app._handle_event(milestone("r1", deep_output=f"Changed greet().\n\n{render_line(entries)}", metrics=entries))
        app._deep.set_run_status("r1", "done")
        # What the terminal result event carries: the same text, the same entries.
        app._answer_parts = [f"Changed greet().\n\n{render_line(entries)}"]
        app._auto_pass = 1

        app._finish_turn("improve the greeting", _FakeMetricFinal(), 3.0, cancelled=False, error=None)

        lines = stripped(log)
        assert lines.count(PAIR_LINE) == 1
        assert not any("Metric:" in ln for ln in lines)
        assert "".join(log.lines).count("Changed greet().") == 1


@pytest.mark.asyncio
async def test_a_goal_that_left_no_milestone_still_shows_its_metrics_at_the_end_of_the_turn():
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log
        app._console = _FakeConsole(log)
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()
        app._answer_parts = []
        app._auto_pass = 1

        class Unmet(_FakeMetricFinal):
            deep_results = [type("R", (), {"met": False, "output": "Tried it.", "tokens": 5,
                                           "metric_results": [MEASURED], "metric_missing": False})()]
            metric_results = [MEASURED]

        app._finish_turn("improve the greeting", Unmet(), 3.0, cancelled=False, error=None)
        lines = stripped(log)
        assert lines.count(MEASURED_LINE) == 1
        closing = next(i for i, ln in enumerate(lines) if "Stopped before finishing" in ln)
        assert lines.index(MEASURED_LINE) < closing, "above the closing line"


@pytest.mark.asyncio
async def test_the_plain_text_line_is_not_printed_when_the_answer_falls_back_to_the_generic_bubble():
    """A deep turn that flushed nothing prints its text as the answer; the library's plain line is not in it."""
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log
        app._console = _FakeConsole(log)
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()
        app._answer_parts = []
        app._auto_pass = 1

        class WithText(_FakeMetricFinal):
            text = f"Changed greet().\n\n{render_line([MEASURED, RECORDED])}"

        app._finish_turn("improve the greeting", WithText(), 3.0, cancelled=False, error=None)
        body = "\n".join(log.lines)
        assert "Changed greet()." in body and "Metric:" not in body
        assert stripped(log).count(PAIR_LINE) == 1
        assert body.index(PAIR_LINE) < body.index("Changed greet()"), "ahead of the answer it would be buried under"


@pytest.mark.asyncio
async def test_the_explanation_line_counts_the_metric_results_and_adds_only_the_lines_not_yet_shown():
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log
        app._console = _FakeConsole(log)
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()
        app._answer_parts = ["Yes."]
        app._auto_pass = 1

        class Answer:
            kind = "answer"
            text = "Yes."
            deep_results = []
            goals = []
            tokens_in = 10
            tokens_out = 20
            model = "claude-sonnet-4-6"
            steps = 2
            metric_results = []
            metric_missing = False
            explanation = {"version": 3, "used": {"sources": [{"label": "Workspace files", "item_count": 1}]},
                           "metrics": [MEASURED], "metric_missing": True}

        app._finish_turn("what changed", Answer(), 1.0, cancelled=False, error=None)
        lines = stripped(log)
        assert "How I got this: 1 source, 1 metric result" in lines
        explain = lines.index("How I got this: 1 source, 1 metric result")
        assert lines[explain + 1] == MEASURED_LINE and lines[explain + 2] == NOT_RECORDED_LINE
        assert lines.count(MEASURED_LINE) == 1


def test_goal_updated_when_real_subgoal_arrives_later():
    """A placeholder goal is replaced once the real subgoal is known."""
    t = _DeepRunTracker()
    t.add_run("r1", "Executing work…")
    t.update_goal("r1", "Investigate the narrator latency")
    with t._lock:
        assert t._runs["r1"]["goal"] == "Investigate the narrator latency"


def test_summarize_exec_lines_counts_and_narration():
    """The summarizer rolls tool ops into counts and returns narration separately."""
    summary, narration = QuestAITerminal._summarize_exec_lines([
        "Read: /a", "Read: /b", "Edit: /a", "$ ls", "WebSearch: x",
        "Using Agent", "[thinking] hmm", "I planned the change.",
    ])
    assert summary == "2 files read · 1 edit · 1 command · 1 search · 1 tool call"
    assert narration == ["I planned the change."]  # thinking is dropped, tool ops are not narration


def test_summarize_exec_lines_collapses_consecutive_identical_narration():
    """A retry loop that fails the SAME way every attempt reports the same line over and over
    (live: a model the worker could not run, six times). Show it once, and say it repeated —
    otherwise it crowds out everything else the run said."""
    err = "There's an issue with the selected model. It may not exist."
    summary, narration = QuestAITerminal._summarize_exec_lines([
        "Starting.", err, "Read: /a", err, "[thinking] hmm", err, "Done trying.",
    ])
    assert summary == "1 file read"
    # The repeats are consecutive as NARRATION (a tool line and a thinking line between them are
    # not narration and must not split the run of identical messages).
    assert narration == ["Starting.", f"{err} (repeated 3 times)", "Done trying."]


def test_summarize_exec_lines_keeps_non_consecutive_repeats():
    """Only CONSECUTIVE repeats collapse: the same line said again after something else happened
    is a real second occurrence, not noise."""
    _, narration = QuestAITerminal._summarize_exec_lines(["Trying.", "Waiting.", "Trying."])
    assert narration == ["Trying.", "Waiting.", "Trying."]


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


def test_with_several_runs_each_record_names_its_own_goal():
    """The goal is the one thing that tells two runs of a turn apart, so it heads each record."""
    app, log = _make_app()
    for rid, goal in (("r1", "Fix the parser"), ("r2", "Update the docs")):
        app._deep.add_run(rid, goal)
        app._deep.update_run_output(rid, "Edit: /a.py")
    app._flush_pending_deep_runs()
    headings = [ln for ln in log.lines if ln in ("Fix the parser", "Update the docs")]
    assert headings == ["Fix the parser", "Update the docs"]
    assert not any("⎅" in ln for ln in log.lines), "no decorative glyph before a goal"
