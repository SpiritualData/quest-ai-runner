"""The prompt input box must stay pinned to the bottom of the Textual terminal,
even when the panels above it (context cards, deep-run dashboard, the expanded
deep-detail panel, future-context) grow tall enough to exceed the viewport.

Regression coverage for a bug report: with no `dock` set on any widget, a tall
stack of panels above the prompt could push it down off-screen, forcing the
user to scroll the whole screen to find the input box (unlike e.g. Claude
Code's terminal UI, where the input always stays put at the bottom).
"""
from __future__ import annotations

import pytest

from quest_ai_runner.textual_ui import QuestAITerminal


class _FakeSession:
    _rep_name = "Tester"
    _console = None
    _cfg = None
    _goal_id = None
    _model_hint = None


def _grow_all_panels(app: QuestAITerminal) -> None:
    """Push every optional panel to its tallest displayed state at once, so the
    stack of widgets above #prompt is taller than a typical small terminal."""
    app._ctx.set_cards([
        {"id": f"c{i}", "title": f"Card {i}", "adapter": "files",
         "relevance_score": 0.9, "files": [f"a/b/file_{i}_{j}.py" for j in range(4)]}
        for i in range(6)
    ])
    app._deep_view.show(
        "\n".join(f"deep run line {i}" for i in range(20)), n_runs=3,
    )
    app._deep_detail.open_for(
        "r1", "A long-running goal", [f"step {i}" for i in range(40)],
    )
    app._future_ctx_panel.load("\n".join(f"- future context line {i}" for i in range(20)))
    app._future_ctx_panel.display = True
    app._future_ctx_panel._rerender()


@pytest.mark.asyncio
async def test_prompt_stays_docked_at_bottom_when_panels_grow():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt")
        footer = app.query_one("Footer")

        # Baseline: even with nothing grown, the prompt sits directly above the
        # (also bottom-docked) Footer -- the true bottom of the usable screen.
        await pilot.pause()
        baseline_bottom = prompt.region.y + prompt.region.height
        assert baseline_bottom == footer.region.y

        # Now grow every panel well past the viewport height.
        _grow_all_panels(app)
        await pilot.pause()

        # The prompt must still end exactly where it started, not be pushed
        # down/off-screen by the now-overflowing panels above it.
        bottom = prompt.region.y + prompt.region.height
        assert bottom == footer.region.y
        assert bottom == baseline_bottom


@pytest.mark.asyncio
async def test_activity_bar_stays_glued_above_prompt_when_panels_grow():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        activity = app.query_one("#activity")
        prompt = app.query_one("#prompt")

        app._activity.display = True
        _grow_all_panels(app)
        await pilot.pause()

        # The activity bar (docked bottom, mounted before #prompt) sits directly
        # above the docked prompt, not pushed elsewhere by the grown panels.
        assert activity.region.y + activity.region.height == prompt.region.y


@pytest.mark.asyncio
async def test_transcript_still_fills_remaining_space_above_docked_widgets():
    """#transcript's height: 1fr must still occupy the space left over once the
    docked bottom widgets (and the other panels) claim theirs -- docking must not
    silently break the flexible-height layout for the transcript."""
    app = QuestAITerminal(_FakeSession())
    async with app.run_test(size=(80, 40)) as pilot:
        transcript = app.query_one("#transcript")
        prompt = app.query_one("#prompt")
        await pilot.pause()

        # Transcript starts right after the header and ends right where the
        # docked prompt begins (nothing else is displayed at this point).
        assert transcript.region.height > 0
        assert transcript.region.y + transcript.region.height <= prompt.region.y
