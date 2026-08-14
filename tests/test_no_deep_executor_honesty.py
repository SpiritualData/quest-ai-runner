"""A turn that executed nothing must never read like a completion report.

Live bug (confirmed in production by a user who asked for a documentation file to be updated):
the final answer bubble of the chat turn read

    Executing: CLAUDE.md's Current situation reflects that committee follow-up is paused ...

which reads exactly like "here is what I did". Nothing had run: no deep executor was configured,
no file was touched, and the only hint was a dim side note above it saying "(No deep executor
configured; cannot auto-execute)".

Three separate defects lined up to produce it, and this file pins all three:

1. ``Orchestrator`` announced "Executing: <goal>" the moment the planner chose to execute, BEFORE
   checking whether anything could execute, and typed the announcement ``EVENT_RESULT`` — the type
   that carries a turn's actual outcome. It is now gated on real execution capability and typed
   ``EVENT_INTENT``.
2. ``_run_deep`` returned its no-executor result with ``text=None``, so the honest explanation
   existed only as one UI's side note. It now carries ``NO_DEEP_EXECUTOR_TEXT``.
3. Both chat UIs fall back to the last result text when a deep turn flushes no output of its own,
   so they picked the interim announcement back up and showed it as the answer.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from quest_ai_runner.core.adapters import (
    EVENT_INTENT,
    EVENT_RESULT,
    SURFACING_EVENTS,
    Mode,
    StreamSink,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    NO_DEEP_EXECUTOR_TEXT,
    Orchestrator,
    OrchestratorConfig,
)
from quest_ai_runner.interactive import _DeepRunTracker
from quest_ai_runner.textual_ui import QuestAITerminal
from tests.conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


# ---------------------------------------------------------------------------
# 1. The brain: no capability -> no claim of execution, and a result that explains itself
# ---------------------------------------------------------------------------

class _RecordingSink(StreamSink):
    """A StreamSink that keeps every event dict the orchestrator emits, in order."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        super().__init__(self.events.append)

    def texts(self, event_type: str) -> List[str]:
        return [(e.get("text") or "") for e in self.events if e.get("type") == event_type]


def _deep_orchestrator(**kwargs: Any) -> tuple[Orchestrator, _RecordingSink]:
    """An orchestrator whose planner always chooses to execute, with NO deep runner wired."""
    provider = StubProvider(decisions=[{
        "action": "deep",
        "goal": "Update CLAUDE.md's Current situation section",
        "deep_brief": "rewrite the Current situation paragraph",
        "rationale": "the user asked for a file change",
    }])
    orch = Orchestrator(
        retrieval=StubRetrieval({"CLAUDE.md": "Current situation: unchanged."}),
        provider=provider,
        registry=ModelRegistry(provider),
        escalation=StubEscalation(),
        config=OrchestratorConfig(max_steps=3),
        **kwargs,
    )
    return orch, _RecordingSink()


def test_no_executing_claim_is_emitted_when_nothing_can_execute():
    orch, sink = _deep_orchestrator()
    orch.run("update the Current situation in CLAUDE.md", mode=Mode.LIVE, sink=sink)

    # The heart of the bug: an "Executing: ..." sentence typed as a RESULT, emitted although no
    # runner existed. Neither the claim nor that typing may appear.
    assert not any(t.startswith("Executing:") for t in sink.texts(EVENT_RESULT)), (
        f"a result event claimed execution: {sink.texts(EVENT_RESULT)!r}")
    assert not any(t.startswith("Executing:") for t in sink.texts(EVENT_INTENT)), (
        "an intent to execute was announced although nothing could execute")


def test_intent_is_announced_when_a_runner_is_actually_wired():
    """The announcement is not simply deleted: with a real runner it still fires, as EVENT_INTENT."""

    orch, sink = _deep_orchestrator(
        deep_runner=StubDeepRunner(met=True, output="edited CLAUDE.md"))
    orch.run("update the Current situation in CLAUDE.md", mode=Mode.LIVE, sink=sink)

    assert any(t.startswith("Executing:") for t in sink.texts(EVENT_INTENT)), (
        "a turn that really executes should still announce what it is about to do")
    assert not any(t.startswith("Executing:") for t in sink.texts(EVENT_RESULT)), (
        "the announcement must never be typed as the turn's result")


def test_no_executor_result_explains_itself():
    orch, sink = _deep_orchestrator()
    res = orch.run("update the Current situation in CLAUDE.md", mode=Mode.LIVE, sink=sink)

    assert res.kind == "deep"
    assert res.deep_results == []            # nothing ran
    assert res.goals                          # but the goals are surfaced
    assert res.text == NO_DEEP_EXECUTOR_TEXT  # and the result says so itself
    assert "did not execute" in res.text
    assert "nothing actually ran" in res.text


def test_intent_is_a_surfacing_event():
    """A BACKGROUND run drops chatter; the intent announcement is not chatter, it is a
    user-facing statement about what the run is doing, so it must reach every sink."""
    assert EVENT_INTENT in SURFACING_EVENTS


# ---------------------------------------------------------------------------
# 2. The Textual UI
# ---------------------------------------------------------------------------

class _RecordingLog:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        if hasattr(x, "plain"):
            self.lines.append(x.plain)
        elif hasattr(x, "markup"):
            self.lines.append(x.markup)
        else:
            self.lines.append(str(x))


class _FakeConsole:
    def __init__(self, log: _RecordingLog) -> None:
        self._log = log
        self.markdown_calls: List[str] = []

    def markdown(self, text: str) -> None:
        self.markdown_calls.append(text)
        self._log.write(text)

    def dim(self, text: str) -> None:
        self._log.write(text)

    def rule(self) -> None:
        pass


class _NoExecutorDeepFinal:
    """What ``Orchestrator._run_deep`` returns with no execution capability wired."""
    kind = "deep"
    text = NO_DEEP_EXECUTOR_TEXT
    deep_results: List[Any] = []
    goals = ["Update CLAUDE.md's Current situation section"]
    tokens_in = 10
    tokens_out = 20
    model = "claude-sonnet-4-6"
    steps = 1


class _FakeSessionForFinishTurn:
    _rep_name = "Tester"
    # deep_runner=None on the config is what the resolved tri-state leaves behind when no worker
    # could be built, which is exactly the state the live bug was reported in.
    _cfg = type("Cfg", (), {"corpus_root": None, "deep_runner": None})()
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: List[Any] = []
        self._turn_count = 0
        self._turns: List[Any] = []
        self._orch = type("Orch", (), {"context_assembler": None})()

    def _write_session_file(self) -> None:
        pass


def _make_terminal() -> tuple[QuestAITerminal, _RecordingLog, _FakeConsole]:
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    app._ev = None
    log = _RecordingLog()
    console = _FakeConsole(log)
    app._tlog = log
    app._console = console
    return app, log, console


def test_textual_intent_event_never_becomes_the_answer():
    """The interim announcement is written to the transcript but kept OUT of ``_answer_parts``,
    which is the pot ``_finish_turn`` falls back to for the turn's answer."""
    app, log, _console = _make_terminal()
    app._answer_parts = []
    app._partial_started = False

    app._handle_event({"type": EVENT_INTENT,
                       "text": "Executing: Update CLAUDE.md's Current situation section"})

    assert app._answer_parts == [], (
        "an announcement of intent must never be a candidate for the turn's answer")
    assert any("Executing: Update CLAUDE.md" in ln for ln in log.lines), (
        "the announcement should still be visible as progress")


@pytest.mark.asyncio
async def test_textual_no_executor_turn_shows_the_honest_text_not_the_announcement():
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        log = _RecordingLog()
        console = _FakeConsole(log)
        app._tlog = log
        app._console = console
        app._deep = _DeepRunTracker()
        app._deep_flushed = set()      # nothing was ever flushed: nothing ran
        app._ev = None
        app._answer_parts = []
        app._partial_started = False
        app._auto_pass = 1

        goal = "Update CLAUDE.md's Current situation section"
        app._handle_event({"type": EVENT_INTENT, "text": f"Executing: {goal}"})
        final = _NoExecutorDeepFinal()
        app._finish_turn("update the Current situation in CLAUDE.md", final, 3.4,
                         cancelled=False, error=None)

        # The bug, stated as an assertion: the announcement must not be the answer.
        assert f"Executing: {goal}" not in console.markdown_calls, (
            "the interim 'Executing: ...' line was rendered as the turn's answer")
        # And the turn does say, in the answer position, that nothing ran.
        assert NO_DEEP_EXECUTOR_TEXT in console.markdown_calls

        # The session history the NEXT turn reads back must not claim an attempt either.
        assert "Attempted" not in app.sess._last_assistant
        assert "NOT executed" in app.sess._last_assistant


# ---------------------------------------------------------------------------
# 3. The plain (ANSI) interactive UI
# ---------------------------------------------------------------------------

def _make_renderer():
    from quest_ai_runner.interactive import _Console, _ContextPanel, _TurnRenderer
    console = _Console()
    console._rich = None
    console._color = False
    lines: List[str] = []
    markdown_calls: List[str] = []
    console.line = lambda s="": lines.append(s)          # type: ignore[assignment]
    console.write = lambda s: lines.append(s)            # type: ignore[assignment]

    def _markdown(s: str) -> None:
        markdown_calls.append(s)
        lines.append(s)

    console.markdown = _markdown                          # type: ignore[assignment]
    panel = _ContextPanel(console)
    renderer = _TurnRenderer(console, panel, "Tester")
    return renderer, lines, markdown_calls


def test_interactive_types_map_includes_intent():
    renderer, _lines, _md = _make_renderer()
    assert renderer._types().get("intent") == EVENT_INTENT


def test_interactive_intent_is_a_progress_line_not_an_answer_bubble():
    renderer, lines, markdown_calls = _make_renderer()
    goal = "Update CLAUDE.md's Current situation section"

    renderer.render({"type": EVENT_INTENT, "text": f"Executing: {goal}"})

    # It shows, so the user still sees what is about to happen...
    assert any(f"Executing: {goal}" in ln for ln in lines)
    # ...but not through the answer path (markdown + the "Tester (AI):" label), which is what
    # made it read as the turn's reply.
    assert markdown_calls == []
    assert not any("Tester (AI):" in ln for ln in lines)
    assert renderer._ai_label_printed is False


def test_interactive_result_still_renders_as_the_answer():
    """The result path is untouched: a real answer still goes through markdown + the AI label."""
    renderer, lines, markdown_calls = _make_renderer()
    renderer.render({"type": EVENT_RESULT, "text": "Here is the real answer."})
    assert markdown_calls == ["Here is the real answer."]
    assert any("Tester (AI):" in ln for ln in lines)
