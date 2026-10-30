"""The transcript keeps the newest output in view, and the header and Alt+C tell the truth.

Three defects of the terminal chat, each found from a captured screen (an independent review):

1. The transcript stopped following new output once the live work panel appeared. It decided
   whether to follow by measuring how far from the bottom the view was; the panel shrinks the
   transcript, so every later line measured as "scrolled away" and the answer, or a question, landed
   below the visible rows. It is now a state: following until the person scrolls up, following again
   when they are back at the bottom.
2. The header and the window title were set to the best-scoring context card's title, presented as
   the quest being worked on.
3. Alt+C did nothing after a short answer, although the closing line offered it, because it looked
   at the deep run's context and not at what the turn had loaded.

The real app runs headless under Textual's ``run_test``; no model, no network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rich.text import Text

from quest_ai_runner.core.adapters_events import EVENT_CONTEXT
from quest_ai_runner.textual_ui import QuestAITerminal

SIZE = (80, 24)


def run_app(build, size=SIZE, session=None):
    """Run ``build(app, pilot)`` against a mounted app with startup skipped."""
    async def go():
        app = QuestAITerminal(session, _config=None)
        app._build_session_worker = lambda: None  # skip the real (network) session build
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


def fill(app, n: int, prefix: str = "line") -> None:
    for i in range(n):
        app._tlog.write(Text(f"{prefix} {i}"))


def rows_below(app) -> int:
    """Transcript rows that exist but sit below the visible rows."""
    log = app._tlog
    return max(0, len(log.lines) - (int(log.scroll_y) + log.scrollable_content_region.height))


# --- the transcript keeps following ----------------------------------------------------------

def test_the_transcript_still_follows_after_the_work_panel_appears():
    async def build(app, pilot):
        fill(app, 60)
        await pilot.pause()
        before = rows_below(app)
        # The live work panel appears: the transcript loses rows, as in a real deep run.
        app._deep_view.show("\x1b[1;36m▸ ⎅ Do the work\x1b[0m\n  ● 1s\n    Using WebSearch\n  → Read: a.md", n_runs=1)
        await pilot.pause()
        app._tlog.write(Text("THE ANSWER"))
        fill(app, 3, "more")
        await pilot.pause()
        return before, rows_below(app), app._tlog.lines[-1].text.strip(), app._tlog.following

    before, after, last, following = run_app(build)
    assert before == 0
    assert after == 0, "the newest lines must stay in view after the panel appeared"
    assert last == "more 2" and following


def test_the_bottom_stays_in_view_when_the_panel_appears_with_nothing_written_after_it():
    async def build(app, pilot):
        fill(app, 60)
        await pilot.pause()
        app._deep_view.show("▸ ⎅ Do the work\n  ● 1s\n    Using WebSearch\n  → Read: a.md", n_runs=1)
        await pilot.pause()
        await pilot.pause()
        return rows_below(app)

    assert run_app(build) == 0


def test_scrolling_up_stops_following_and_returning_to_the_bottom_resumes_it():
    async def build(app, pilot):
        log = app._tlog
        fill(app, 80)
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()
        scrolled_to = log.scroll_y
        was_following = log.following
        # New output while the person reads earlier lines: the view stays where they put it.
        fill(app, 5, "new")
        await pilot.pause()
        stayed = log.scroll_y == scrolled_to
        # The work panel appearing or going away does not pull them back either.
        app._deep_view.show("▸ ⎅ Do the work\n  ● 1s\n    Using WebSearch", n_runs=1)
        await pilot.pause()
        app._deep_view.hide()
        await pilot.pause()
        still_there = log.scroll_y <= scrolled_to + 1 and not log.following
        for _ in range(60):
            if log.following:
                break
            await pilot.press("pagedown")
            await pilot.pause()
        resumed = log.following
        fill(app, 2, "after")
        await pilot.pause()
        return (was_following, stayed, still_there, resumed, rows_below(app), log.lines[-1].text.strip())

    was_following, stayed, still_there, resumed, below, last = run_app(build)
    assert not was_following, "PgUp stops following"
    assert stayed, "output arriving while scrolled up does not move the view"
    assert still_there, "a panel appearing or leaving does not pull a reader to the bottom"
    assert resumed and below == 0 and last == "after 1", "back at the bottom, following resumes"


def test_a_log_shorter_than_its_view_always_follows():
    async def build(app, pilot):
        fill(app, 3)
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()
        return app._tlog.following

    assert run_app(build) is True


# --- the header says what the app is, not which card scored highest ---------------------------

def test_a_context_event_does_not_retitle_the_app_after_its_best_card():
    titled = []

    async def build(app, pilot):
        app._set_terminal_title = lambda title: titled.append(title)
        before = app.title
        app._handle_event({"type": EVENT_CONTEXT, "data": {
            "card_metadata": [{"id": "c1", "title": "Quest AI Chat Experience", "relevance_score": 0.93},
                              {"id": "c2", "title": "Goal: Become an engineer", "relevance_score": 0.81}],
            "sources": []}})
        await pilot.pause()
        return before, app.title

    before, after = run_app(build)
    assert titled == [], "no card title reaches the header or the window title"
    assert before == after == "Quest AI Runner"


# --- Alt+C opens what the turn loaded ----------------------------------------------------------

class StubSession:
    """The part of a session ``_finish_turn`` touches."""
    _rep_name = "Tester"
    _cfg = SimpleNamespace(corpus_root=None, deep_runner=None)
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self._orch = SimpleNamespace(context_assembler=None, input_inbox=None)

    def _write_session_file(self) -> None:
        pass

    def _maybe_refresh_next_steps(self, final) -> None:
        pass


class ShortAnswer:
    kind = "answer"
    text = "I can do that. First I need the sheet link."
    deep_results: list = []
    goals: list = []
    steps = 2
    model = "sonnet"
    tokens_in = 0
    tokens_out = 0


def test_alt_c_opens_the_context_a_short_answer_used():
    async def build(app, pilot):
        app._ctx.set_cards([{"id": "c1", "title": "Pricing tiers", "adapter": "keyword"},
                            {"id": "c2", "title": "Refund policy", "adapter": "vector"}])
        app._future_context = ""   # a short answer has no deep run's context
        app._finish_turn("what are the tiers?", ShortAnswer(), 4.0, cancelled=False, error=None)
        await pilot.pause()
        shown_before = app._future_ctx_panel.display
        await pilot.press("alt+c")
        await pilot.pause()
        opened = app._future_ctx_panel.display
        await pilot.press("alt+c")
        await pilot.pause()
        return shown_before, opened, app._future_ctx_panel.display

    before, opened, closed = run_app(build, session=StubSession())
    assert before is False and opened is True and closed is False


def test_alt_c_does_nothing_when_the_turn_loaded_no_context():
    async def build(app, pilot):
        app._ctx.reset()
        app._future_context = ""
        app._finish_turn("hello", ShortAnswer(), 1.0, cancelled=False, error=None)
        await pilot.pause()
        await pilot.press("alt+c")
        await pilot.pause()
        return app._future_ctx_panel.display

    assert run_app(build, session=StubSession()) is False


def test_the_header_line_joins_title_and_sub_title_with_a_dot_not_a_dash():
    async def build(app, pilot):
        return (app.format_title("Quest AI Runner", "Resolving AI persona…").plain,
                app.format_title("Quest AI Runner", "").plain)

    with_sub, without = run_app(build)
    assert with_sub == "Quest AI Runner · Resolving AI persona…"
    assert without == "Quest AI Runner"
    assert "—" not in with_sub
