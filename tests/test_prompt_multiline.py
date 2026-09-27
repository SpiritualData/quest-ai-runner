"""The terminal prompt shows every row of a multi-line message and inserts newlines without sending.

Two regressions this guards:
1. The box sized itself from hard newlines only, so text that soft-wrapped onto several rows kept
   a one-row box and showed just the last row.
2. Most terminals send the same carriage return for Shift+Enter as for Enter, so the message was
   submitted. The prompt now also takes Ctrl+J, Alt+Enter and a backslash before Enter, including
   VS Code's backslash, CR, LF rendering of Shift+Enter, as a newline.

Rendered under ``run_test()`` with the app's real CSS, so the border and compact styling are the
ones a user sees.
"""

from __future__ import annotations

import pytest
from textual import events
from textual.app import App
from textual.containers import Vertical
from textual.widgets import Static

from quest_ai_runner.textual_ui import PromptTextArea, QuestAITerminal

LONG = "the quick brown fox jumps over the lazy dog " * 4


class PromptHarness(App):
    CSS = QuestAITerminal.CSS

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self):
        yield Static("transcript", id="transcript")
        with Vertical(id="bottom-bar"):
            yield PromptTextArea(
                id="prompt",
                soft_wrap=True,
                tab_behavior="focus",
                show_line_numbers=False,
                compact=True,
                placeholder="Ask anything",
            )

    def on_prompt_text_area_submitted(self, event: PromptTextArea.Submitted) -> None:
        self.submitted.append(event.value)


async def typed(pilot, text: str) -> None:
    await pilot.press(*["space" if c == " " else c for c in text])
    await pilot.pause()


def content_rows(prompt: PromptTextArea) -> int:
    return prompt.size.height


@pytest.mark.asyncio
async def test_wrapped_text_grows_the_box_to_show_every_row():
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await pilot.pause()
        assert content_rows(prompt) == 1

        await typed(pilot, LONG)
        await pilot.pause()
        assert "\n" not in prompt.text
        assert prompt.wrapped_document.height >= 3
        assert content_rows(prompt) == prompt.wrapped_document.height
        assert prompt.scroll_y == 0


@pytest.mark.asyncio
async def test_box_stops_at_max_lines_and_shrinks_when_cleared():
    app = PromptHarness()
    async with app.run_test(size=(60, 30)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        for i in range(12):
            await typed(pilot, f"line{i}")
            await pilot.press("ctrl+j")
        await pilot.pause()
        assert content_rows(prompt) == PromptTextArea.MAX_LINES

        prompt.clear()
        await pilot.pause()
        await pilot.pause()
        assert content_rows(prompt) == 1


@pytest.mark.asyncio
async def test_narrower_terminal_rewraps_and_regrows():
    app = PromptHarness()
    async with app.run_test(size=(100, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, LONG)
        wide_rows = content_rows(prompt)

        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        await pilot.pause()
        assert content_rows(prompt) > wide_rows
        assert content_rows(prompt) == prompt.wrapped_document.height


@pytest.mark.asyncio
@pytest.mark.parametrize("newline_key", ["shift+enter", "alt+enter", "ctrl+j"])
async def test_newline_keys_insert_a_newline_without_submitting(newline_key):
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "first")
        await pilot.press(newline_key)
        await typed(pilot, "second")
        assert prompt.text == "first\nsecond"
        assert app.submitted == []
        assert content_rows(prompt) == 2


@pytest.mark.asyncio
async def test_backslash_enter_is_a_newline():
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "first")
        await pilot.press("backslash", "enter")
        await typed(pilot, "second")
        assert prompt.text == "first\nsecond"
        assert app.submitted == []


@pytest.mark.asyncio
async def test_vscode_shift_enter_sequence_gives_exactly_one_newline():
    """VS Code's /terminal-setup sends Shift+Enter as backslash, CR, LF in one burst."""
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "first")
        # One read from the terminal: the parser stamps all three keys at the same moment.
        for key, char in (("backslash", "\\"), ("enter", "\r"), ("ctrl+j", "\n")):
            prompt.post_message(events.Key(key, char))
        await pilot.pause()
        await typed(pilot, "second")
        assert prompt.text == "first\nsecond"
        assert app.submitted == []


@pytest.mark.asyncio
async def test_a_later_ctrl_j_after_backslash_enter_still_adds_a_newline():
    """Only the LF inside the same keypress is dropped; a deliberate Ctrl+J afterwards is kept."""
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "first")
        await pilot.press("backslash", "enter")
        await pilot.pause(0.2)
        await pilot.press("ctrl+j")
        await typed(pilot, "second")
        assert prompt.text == "first\n\nsecond"


@pytest.mark.asyncio
async def test_enter_submits_the_whole_multiline_message():
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "first")
        await pilot.press("ctrl+j")
        await typed(pilot, "second")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["first\nsecond"]


@pytest.mark.asyncio
async def test_backslash_not_before_cursor_does_not_turn_enter_into_newline():
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.focus()
        await typed(pilot, "a")
        await pilot.press("backslash")
        await typed(pilot, "b")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["a\\b"]


class FakeShift:
    def __init__(self, held: bool) -> None:
        self.held = held

    def shift_held(self) -> bool:
        return self.held


@pytest.mark.asyncio
async def test_enter_with_shift_physically_held_is_a_newline():
    """GNOME Terminal sends Shift+Enter as a plain CR; the keyboard device says Shift is down."""
    app = PromptHarness()
    async with app.run_test(size=(60, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptTextArea)
        prompt.shift_probe = FakeShift(True)
        prompt.focus()
        await typed(pilot, "first")
        await pilot.press("enter")
        prompt.shift_probe = FakeShift(False)
        await typed(pilot, "second")
        assert prompt.text == "first\nsecond"
        assert app.submitted == []

        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["first\nsecond"]
