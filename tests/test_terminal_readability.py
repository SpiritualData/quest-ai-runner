"""The terminal chat reads well at 80 columns, in plain words, and in colours a person can read.

A second round on an independent review of the terminal chat (the first fixed what it got wrong: the
ending of a turn, the transcript following new output). This one pins the readability findings:

* the key line under the prompt: Esc first, Tab only when there is more than one job, whole hints only;
* the context counted in one word ("sources") wherever it appears;
* "Understood as" restyled so it is not taken for the person's own line;
* no internal word ("deep run", "card", "corpus", "QAR", ...) in anything a person reads by default;
* one time format and one clock; the transcript never narrower than its own lines;
* every text colour that carries information at 4.5:1 or more on its background.

The app is driven through Textual's headless ``run_test``; no model, no network.
"""
from __future__ import annotations

import asyncio
import html
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from quest_ai_runner.core.adapters import (
    DeepResult, EVENT_CONTEXT, EVENT_EXEC, EVENT_PLAN, EVENT_STATUS, EVENT_UNDERSTANDING, ProgressEvent,
)
from quest_ai_runner.core.inbox import InMemoryInbox
from quest_ai_runner.core.orchestrator import OrchestratorResult
from quest_ai_runner.interactive_session_tracker import _DeepRunTracker
from quest_ai_runner.textual_ui import QuestAITerminal
from quest_ai_runner.textual_ui_helpers import context_block_lines, waiting_note_line
from quest_ai_runner.textual_ui_widgets import (
    _AWAITING_DECISION_PLACEHOLDER, _BUSY_PLACEHOLDER, _READY_PLACEHOLDER, KeyHints,
)

# The words a person should never meet in the default view (they stay in the log, Alt+D and -v).
INTERNAL_TERMS = ("deep run", "deep worker", "deep task", "QAR", "card", "corpus", "[Pass", "claude-")


class Session:
    """The part of a session the app touches after startup."""
    _rep_name = "Quest AI"
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self.inbox = InMemoryInbox()
        self._cfg = SimpleNamespace(corpus_root="/srv/files/workspace", deep_runner=object())
        self._orch = SimpleNamespace(context_assembler=None, input_inbox=self.inbox)
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self.resumed = None

    def _write_session_file(self) -> None:
        pass

    def _maybe_refresh_next_steps(self, final) -> None:
        pass


class Log:
    """Everything written to the transcript, as plain lines."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(getattr(x, "plain", None) or getattr(x, "markup", str(x)))


class Console:
    def __init__(self, log: Log) -> None:
        self.log = log

    def markdown(self, text: str) -> None:
        self.log.lines.append(text)

    def dim(self, text: str) -> None:
        self.log.lines.append(text)

    def line(self, text: str = "") -> None:
        self.log.lines.append(text)

    def rule(self) -> None:
        self.log.lines.append("-----")


def run_app(build, size: Tuple[int, int] = (80, 24), session: Any = None):
    """Mount the app with a session already built, run ``build(app, pilot)``, return what it returns."""
    async def go():
        app = QuestAITerminal(session or Session())
        app._build_session_worker = lambda: None
        app._run_stream = lambda user_text: None
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


def event(kind: str, text: str = "", **kwargs) -> ProgressEvent:
    return ProgressEvent(type=kind, text=text, **kwargs)


def cards(n: int = 6) -> List[dict]:
    return [{"id": f"card:area-{i}", "title": f"Source number {i}", "adapter": "files", "relevance_score": 0.5 + i / 20,
             "files": [f"/srv/files/area/file_{i}.md"]} for i in range(n)]


# --- the key line under the prompt ------------------------------------------------------------------

def keys_drawn(app: QuestAITerminal) -> List[str]:
    return [piece for piece in re.split(r"\s{2,}", app.query_one(KeyHints).render().plain.strip()) if piece]


def test_the_keys_start_with_the_one_that_stops_the_work_in_the_order_cancel_details_sources_scroll():
    async def build(app, pilot):
        app._turn_active = True
        app._deep.add_run("r1", "Do the work")
        app._deep.update_run_output("r1", "Read: a.md")
        app._future_ctx_panel.load("- Source number 1")
        return app.key_hints(), keys_drawn(app)

    hints, drawn = run_app(build)
    assert [key for key, _ in hints] == ["esc", "alt+d", "alt+c", "pgup/pgdn", "ctrl+c"]
    assert drawn == ["esc Cancel", "alt+d Details", "alt+c Sources", "pgup/pgdn Scroll", "ctrl+c Copy / Quit"]


def test_tab_is_shown_only_when_there_is_more_than_one_job_to_cycle_through():
    async def build(app, pilot):
        app._deep.add_run("r1", "First")
        app._deep.update_run_output("r1", "Read: a.md")
        one = [key for key, _ in app.key_hints()]
        app._deep.add_run("r2", "Second")
        two = [key for key, _ in app.key_hints()]
        return one, two

    one, two = run_app(build)
    assert "tab" not in one and "alt+d" in one
    assert two == ["alt+d", "tab", "pgup/pgdn", "ctrl+c"]


def test_a_key_that_would_do_nothing_is_not_listed_and_esc_only_while_the_ai_works():
    async def build(app, pilot):
        idle = [key for key, _ in app.key_hints()]
        app._turn_active = True
        busy = [key for key, _ in app.key_hints()]
        return idle, busy

    idle, busy = run_app(build)
    assert idle == ["pgup/pgdn", "ctrl+c"]
    assert busy[0] == "esc"
    assert "ctrl+y" not in busy and "alt+y" not in busy


@pytest.mark.parametrize("width", [60, 80, 120])
def test_the_key_line_shows_whole_hints_and_never_a_key_cut_in_half(width):
    async def build(app, pilot):
        app._turn_active = True
        app._deep.add_run("r1", "First")
        app._deep.add_run("r2", "Second")
        app._deep.update_run_output("r1", "Read: a.md")
        app._future_ctx_panel.load("- Source number 1")
        await pilot.pause()
        return keys_drawn(app), app.query_one(KeyHints).render().plain, app.query_one(KeyHints).size.width

    drawn, plain, room = run_app(build, size=(width, 24))
    assert len(plain) <= room
    assert drawn[0] == "esc Cancel", "the key that stops the work is always the first one drawn"
    wanted = ["esc Cancel", "alt+d Details", "alt+c Sources", "tab Next job", "pgup/pgdn Scroll", "ctrl+c Copy / Quit"]
    assert drawn == wanted[:len(drawn)], "a hint that does not fit is left out whole, from the end"
    if width >= 80:
        assert len(drawn) >= 5


def test_ctrl_y_is_not_bound_to_a_copy_the_prompts_own_redo_would_shadow_and_the_copy_has_a_key_that_works():
    keys = {b.key: b.action for b in QuestAITerminal.BINDINGS}
    assert "ctrl+y" not in keys
    assert keys["alt+y"] == "copy_last"

    async def build(app, pilot):
        app._answer_parts = ["The last reply."]
        copied = []
        app._copy_text = lambda text: (copied.append(text), (True, "clipboard"))[1]
        app.query_one("#prompt").focus()
        await pilot.press("alt+y")
        await pilot.pause()
        return copied

    assert run_app(build) == ["The last reply."]


def test_the_busy_placeholder_names_the_key_that_stops_the_work_and_the_ready_one_fits_at_80_columns():
    assert "Esc" in _BUSY_PLACEHOLDER and _BUSY_PLACEHOLDER == "Type to add a note for the AI. Esc stops the work."

    async def build(app, pilot):
        prompt = app.query_one("#prompt")
        return prompt.content_region.width

    room = run_app(build)
    assert len(_READY_PLACEHOLDER) <= room, (len(_READY_PLACEHOLDER), room)
    assert len(_BUSY_PLACEHOLDER) <= room and len(_AWAITING_DECISION_PLACEHOLDER) <= room


def test_the_key_line_sits_directly_under_the_prompt_in_one_row():
    async def build(app, pilot):
        prompt, keys = app.query_one("#prompt"), app.query_one(KeyHints)
        return prompt.region.y + prompt.region.height, keys.region.y, keys.region.height, app.size.height

    prompt_bottom, keys_top, keys_height, screen_height = run_app(build)
    assert prompt_bottom == keys_top and keys_height == 1 and keys_top + 1 == screen_height


# --- "Understood as" is the AI's, not the person's own line -----------------------------------------

def test_understood_as_is_not_the_style_of_the_persons_own_line_and_keeps_its_text():
    async def build(app, pilot):
        log = Log()
        styles: Dict[str, str] = {}

        class Styled(Log):
            def write(self, x) -> None:  # noqa: ANN001
                super().write(x)
                styles[getattr(x, "plain", "")] = str(getattr(x, "style", ""))

        app._tlog = Styled()
        app._begin_turn("Help me with the sheet", echo=True)
        app._handle_event(event(EVENT_UNDERSTANDING, "Understood as: A sheet of roles exists."))
        return styles

    styles = run_app(build)
    own = styles["❯ Help me with the sheet"]
    understood = styles["  ◆ Understood as: A sheet of roles exists."]
    assert own == "bold cyan"
    assert understood != own and "bold" not in understood and "cyan" not in understood
    assert "italic" in understood, "the AI's voice, like its other lines"


def test_a_deep_plan_writes_no_deep_execution_line_and_says_getting_ready_in_plain_words():
    async def build(app, pilot):
        log = Log()
        app._tlog = log
        app._begin_turn("Do it", echo=False)
        before = len(log.lines)
        app._handle_event(event(EVENT_PLAN, "internal routing rationale", action="deep"))
        return log.lines[before:], app._activity.render().plain

    written, status = run_app(build)
    assert written == [], "no 'Deep execution' line, and the routing rationale is not echoed"
    assert status.endswith("Getting ready to start the work…")


# --- one vocabulary and one count for the context ---------------------------------------------------

def test_the_context_is_counted_in_sources_everywhere_and_the_counts_agree():
    final = OrchestratorResult(kind="answer", text="Here you go.", steps=2, model="sonnet")
    final.explanation = {"version": 3, "used": {"cards": [{"title": f"Source number {i}", "adapter": "files"}
                                                           for i in range(6)], "sources": [], "reads": [{}] * 4,
                                                "actions": [], "web": False}}

    async def build(app, pilot):
        log = Log()
        app._tlog = log
        app._console = Console(log)
        app._begin_turn("What is in the files?", echo=True)
        app._handle_event(event(EVENT_CONTEXT, "", data={"card_metadata": cards(6), "sources": [
            {"label": "Workspace files", "adapter": "files", "items": ["a", "b"]}]}))
        app._auto_pass = 1
        app._finish_turn("What is in the files?", final, 4.0, False, None)
        return "\n".join(log.lines), app._future_ctx_panel._bullets

    text, bullets = run_app(build, session=Session())
    assert "Found 6 sources: Source number 5, Source number 4 and 4 more." in text
    assert "6 sources" in text.split("How I got this:")[1].splitlines()[0]
    assert text.count("6 sources") == 3, "the context line, the closing line and the explanation say the same number"
    assert len([ln for ln in bullets.splitlines() if ln.strip()]) == 6, "Alt+C lists the same six"
    assert "Alt+C sources used" in text
    for stray in ("card", "reads", "2 sources"):
        assert stray not in text, stray


def test_the_context_line_never_names_an_id_and_at_most_two_titles():
    (line,) = context_block_lines([{"id": "card:area-123", "title": f"T{i}", "relevance_score": i} for i in range(9)], [])
    assert line.plain.count("T") == 2 and "card:" not in line.plain and line.plain.endswith("and 7 more.")


# --- the customer wording, and no internal word in the default view ----------------------------------

def test_nothing_a_person_reads_by_default_contains_an_internal_word():
    """A turn from the request to the closing line, every string it puts on screen, scanned."""
    final = OrchestratorResult(
        kind="deep", goals=["Add the rows"], steps=3, model="sonnet",
        deep_results=[DeepResult(met=True, output="Added 14 rows.", model="sonnet", resolved_model="claude-sonnet-5-5",
                                 tokens=100)])

    async def build(app, pilot):
        log = Log()
        app._tlog = log
        app._console = Console(log)
        seen: List[str] = []
        app._begin_turn("Add the rows", echo=True)
        seen.append(app._activity.render().plain)
        for ev in (event(EVENT_UNDERSTANDING, "Understood as: Rows exist."),
                   event(EVENT_CONTEXT, "", data={"card_metadata": cards(), "sources": [
                       {"label": "Workspace files", "adapter": "files", "items": ["a"]}]}),
                   event(EVENT_PLAN, "x", action="deep"),
                   event(EVENT_STATUS, "Gathering what I need to start"),
                   event(EVENT_STATUS, "Checking the starting value before any change."),
                   event(EVENT_STATUS, "Starting the work on sonnet."),
                   event(EVENT_EXEC, "Read: notes.md", data={"run_id": "r1", "goal": "Add the rows"}),
                   event(EVENT_STATUS, "The work ran on sonnet.", data={"deep_run_model": {"tier": "sonnet"}})):
            app._handle_event(ev)
            seen.append(app._activity.render().plain)
        seen.append(app._deep_view.render().plain)
        seen.append(app.query_one("#prompt").placeholder)
        app._auto_pass = 1
        app._finish_turn("Add the rows", final, 212.0, False, None)
        seen += [h for pair in app.key_hints() for h in pair]
        seen.append(app.query_one("#prompt").placeholder)
        seen.append(app._future_ctx_panel._body.render().plain if app._future_ctx_panel.display else "")
        return seen, log.lines

    seen, lines = run_app(build)
    everything = "\n".join(seen + lines)
    assert "Getting ready to start the work…" in everything and "Starting the work on sonnet." in everything
    assert "The work ran on" not in everything, "the model is named once in the turn: on the closing line"
    found = [term for term in INTERNAL_TERMS if term.lower() in everything.lower()]
    assert found == [], (found, everything)


def test_the_status_strings_say_the_work_and_never_the_deep_run():
    from quest_ai_runner.core.launch_failure import status_text
    text = status_text(DeepResult(met=False, launch_failed=True, launch_problem="missing_program",
                                  error="The deep worker could not start: binary not found (claude)."))
    assert text == ("Could not start the work: the program that runs the work is not installed on this machine, "
                    "or is not on its PATH.")
    assert not [term for term in INTERNAL_TERMS if term.lower() in text.lower()]


# --- one time format, one clock ---------------------------------------------------------------------

def test_a_plain_answers_closing_line_writes_its_time_the_way_every_other_time_is_written():
    final = OrchestratorResult(kind="answer", text="Done.", steps=2, model="sonnet", tokens_in=10, tokens_out=20)

    async def build(app, pilot):
        log = Log()
        app._tlog = log
        app._console = Console(log)
        app._auto_pass = 1
        app._finish_turn("hi", final, 9.0, False, None)
        app._finish_turn("hi again", final, 212.4, False, None)
        return [ln for ln in log.lines if "steps" in ln]

    first, second = run_app(build)
    assert first.rstrip().endswith("9s") and "9.0s" not in first
    assert second.rstrip().endswith("3m32s")


def test_the_work_panels_clock_is_the_turns_clock_not_a_second_one():
    now = [1000.0]

    async def build(app, pilot):
        import quest_ai_runner.interactive_session_tracker as tracker
        import quest_ai_runner.textual_ui_turn as turn
        fake_time = SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0])
        original = (tracker.time, turn.time)
        tracker.time = turn.time = fake_time
        try:
            app._activity.clock = lambda: now[0]
            app._begin_turn("Do it", echo=False)
            now[0] = 1121.0
            app._handle_event(event(EVENT_EXEC, "Read: a.md", data={"run_id": "r1", "goal": "Do it"}))
            now[0] = 1150.0
            return app._deep.get_dashboard(), app._deep.get_dashboard().count("●")
        finally:
            tracker.time, turn.time = original

    dashboard, _ = run_app(build)
    assert "● 2m30s" in dashboard, "150 seconds into the turn, though the worker started at 121"


# --- the transcript keeps its width and the live panel grows with its text --------------------------

def test_the_transcript_is_as_wide_with_a_panel_open_as_without_and_has_no_sideways_scrollbar():
    async def build(app, pilot):
        from rich.text import Text
        log = app._tlog
        for i in range(30):
            log.write(Text(f"line {i} " + "word " * (i % 20)))
        await pilot.pause()
        before = (log.region.width, log.scrollable_content_region.width)
        app._deep_view.show("\x1b[1;36m▸ goal\x1b[0m\n  ● 2m30s\n  → Read: a.md", n_runs=1)
        await pilot.pause()
        after = (log.region.width, log.scrollable_content_region.width)
        return before, after, log.show_horizontal_scrollbar

    before, after, sideways = run_app(build)
    assert before == after, "lines are wrapped to the width they were written at, so it must not change under them"
    assert sideways is False


def test_the_live_panel_grows_when_its_text_does():
    async def build(app, pilot):
        app._deep_view.show("▸ goal\n  ● 1s", n_runs=1)
        await pilot.pause()
        small = app._deep_view.region.height
        app._deep_view.show("▸ goal\n  ● 1s\n" + "\n".join(f"  → Read: file_{i}.md" for i in range(6)), n_runs=1)
        await pilot.pause()
        return small, app._deep_view.region.height

    small, grown = run_app(build)
    assert grown > small, (small, grown)


# --- startup: one plain line, one Ready. ------------------------------------------------------------

def test_a_fresh_start_prints_the_session_line_and_one_ready_without_the_word_corpus():
    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            app._finish_startup(Session())
            await pilot.pause()
            return "\n".join(str(line.text) for line in app._tlog.lines)

    text = asyncio.run(go())
    assert "AI: Quest AI  •  folder: workspace" in text and text.count("Ready.") == 1
    assert "corpus" not in text.lower().split("ready.")[0].split("quest ai  •")[1]
    assert text.index("AI: Quest AI") < text.index("Ready.")


def test_a_command_typed_before_the_session_is_ready_says_what_happens_next():
    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            prompt.text = "/quests"
            await pilot.press("enter")
            await pilot.pause()
            prompt.text = "What is on my plate?"
            await pilot.press("enter")
            await pilot.pause()
            return "\n".join(str(line.text) for line in app._tlog.lines), app.sub_title

    text, _ = asyncio.run(go())
    assert "Still starting up. /quests will run as soon as I'm ready." in text
    assert "❯ What is on my plate?  (waiting)" in text


def test_the_header_says_resuming_with_the_ellipsis_character_the_rest_of_the_app_uses():
    from quest_ai_runner import chat_conversations
    conv = chat_conversations.SavedConversation(path=None, history=[("a", "b")], meta={"rep_name": "Quest AI"})

    async def go():
        app = QuestAITerminal(None, _config=None, resume=conv)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            return app.sub_title

    assert asyncio.run(go()) == "Resuming conversation…"


# --- contrast: every text colour that carries information reaches 4.5:1 ------------------------------

STYLE_RE = re.compile(r"\.terminal-\d+-r(\d+)\s*\{([^}]*)\}")
TEXT_RE = re.compile(r'<text class="terminal-\d+-(r\d+|title)"([^>]*?)>(.*?)</text>', re.S)
RECT_RE = re.compile(r'<rect fill="(#[0-9a-fA-F]{6})" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"')
ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
CELL_W, CELL_H = 12.2, 24.4
MINIMUM_CONTRAST = 4.5


def luminance(value: str) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (int(value.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def text_colours(svg: str) -> List[Tuple[str, str, str]]:
    """(text, foreground, background) of every run of the screen that has a letter or a digit in it.

    Read from Textual's own SVG export: the ``fill`` of each text run and the ``rect`` drawn under it. Runs of only
    borders, scrollbars and block glyphs are decoration, not information, and are not measured."""
    styles: Dict[str, str] = {}
    for num, body in STYLE_RE.findall(svg):
        fill = re.search(r"fill:\s*(#[0-9a-fA-F]{6})", body)
        styles["r" + num] = fill.group(1) if fill else "#c5c8c6"
    rects: Dict[int, List[Tuple[float, float, str]]] = {}
    for fill, x, y, width, height in RECT_RE.findall(svg):
        rects.setdefault(int(round((float(y) - 1.5) / CELL_H)), []).append((float(x), float(x) + float(width), fill))
    runs: List[Tuple[str, str, str]] = []
    for cls, attrs, body in TEXT_RE.findall(svg):
        if cls == "title":
            continue
        a = dict(ATTR_RE.findall(attrs))
        row = re.search(r"line-(\d+)\)", a.get("clip-path", ""))
        text = html.unescape(body).replace("\xa0", " ")
        if not row or not re.search(r"[A-Za-z0-9]", text):
            continue
        x = float(a["x"])
        bg = "#1e1e1e"
        for x0, x1, fill in rects.get(int(row.group(1)), []):
            if x0 - 0.01 <= x < x1 - 0.01:
                bg = fill
        runs.append((text.strip(), styles.get(cls, "#c5c8c6"), bg))
    return runs


def low_contrast(app: QuestAITerminal) -> List[Tuple[float, str, str, str]]:
    found = []
    for text, fg, bg in text_colours(app.export_screenshot()):
        ratio = contrast(fg, bg)
        # A single cell in inverse video is the prompt's cursor.
        if ratio < MINIMUM_CONTRAST and not (len(text) == 1 and luminance(bg) > 0.5):
            found.append((round(ratio, 2), fg, bg, text[:50]))
    return found


def test_the_contrast_function_agrees_with_the_values_the_reviewer_measured():
    assert round(contrast("#777777", "#1e1e1e"), 2) == 3.72
    assert round(contrast("#9e9e9e", "#1e1e1e"), 2) == 6.22
    assert round(contrast("#7b8287", "#242f38"), 2) == 3.50


def test_the_contrast_check_sees_a_colour_that_is_too_faint():
    from rich.text import Text

    async def build(app, pilot):
        app._tlog.write(Text("a faint hint nobody can read", style="#777777"))
        await pilot.pause()
        return low_contrast(app)

    found = run_app(build)
    assert [(ratio, text) for ratio, fg, bg, text in found] == [(3.72, "a faint hint nobody can read")]


def test_every_text_colour_that_carries_information_reaches_4_5_to_1():
    """A turn at 80 columns in each state that has its own colours, the whole screen measured each time."""
    final = OrchestratorResult(
        kind="deep", goals=["Add the rows"], steps=3, model="sonnet",
        deep_results=[DeepResult(met=True, output="Added 14 rows.", model="sonnet", tokens=100)])
    final.metric_results = [{"solution_id": "s", "metric_id": "m", "name": "Pass rate", "before": 0.5, "after": 0.75,
                             "source": "measured", "disagrees": False}]

    async def build(app, pilot):
        failures: Dict[str, list] = {}

        async def measure(state: str) -> None:
            await pilot.pause()
            app._activity.refresh()
            await pilot.pause()
            failures[state] = low_contrast(app)

        await measure("idle")
        app._begin_turn("Help me with the sheet of roles", echo=True)
        app._handle_event(event(EVENT_UNDERSTANDING, "Understood as: A sheet of roles exists."))
        app._handle_event(event(EVENT_CONTEXT, "", data={"card_metadata": cards(), "sources": []}))
        app._handle_event(event(EVENT_STATUS, "Checking the starting value before any change."))
        app._activity.set_queued(2)
        app._tlog.write(waiting_note_line("Also check staging."))
        await measure("busy with a note waiting")
        app._handle_event(event(EVENT_EXEC, "Read: notes.md", data={"run_id": "r1", "goal": "Add the rows"}))
        app._handle_event(event(EVENT_EXEC, "$ python3 sheets.py --append", data={"run_id": "r1", "goal": "Add the rows"}))
        await measure("the work panel")
        app.action_toggle_deep_detail()
        await measure("the details panel (Alt+D)")
        app.action_toggle_deep_detail()
        app._auto_pass = 1
        app._finish_turn("Help me with the sheet of roles", final, 212.0, False, None)
        await measure("after the turn, ready")
        app.action_toggle_future_context()
        await measure("the sources panel (Alt+C)")
        app._awaiting_decision = True
        app.query_one("#prompt").placeholder = _AWAITING_DECISION_PLACEHOLDER
        app.query_one("#prompt").add_class("-awaiting")
        await measure("waiting for an answer")
        return failures

    failures = run_app(build, session=Session())
    assert {state: found for state, found in failures.items() if found} == {}
