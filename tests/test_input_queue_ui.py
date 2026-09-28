"""Terminal input queuing: messages typed while a turn is running.

Claude-Code-like behavior the chat must have (task-reported bug: a message typed mid-turn was
shown as "queued" but then silently dropped once the turn ended):

1. A message queued while the AI works is never simply lost. Whatever the orchestrator's own
   goal loop doesn't drain and fold into ITS run mid-flight is picked up and acted on the moment
   the turn ends (``QuestAITerminal._finish_turn``), by starting a new turn with it.
2. Multiple messages queued during one turn are combined into ONE next turn (joined in the order
   typed), not replayed one at a time.
3. Escape (cancel) reaches the exact same flush path: the interrupted turn's ``_finish_turn`` runs
   immediately with ``cancelled=True``, so anything queued starts right away instead of waiting.
4. The busy UI (the prompt placeholder, the activity strip) shows a live queued count so the user
   knows it's safe to keep typing.

No network: a real ``InMemoryInbox`` (the same object production wires as
``Orchestrator.input_inbox``) plus a minimal stand-in for ``InteractiveSession`` — only the
attributes ``_finish_turn`` and the queuing branch of ``on_prompt_text_area_submitted`` actually
touch.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from quest_ai_runner.core.inbox import InMemoryInbox
from quest_ai_runner.core.orchestrator import OrchestratorResult
from quest_ai_runner.textual_ui import ActivityBar, PromptTextArea, QuestAITerminal, _busy_placeholder


def _stub_session(inbox: InMemoryInbox) -> SimpleNamespace:
    return SimpleNamespace(
        _orch=SimpleNamespace(input_inbox=inbox, context_assembler=None),
        _last_user="", _last_assistant="",
        _session_history=[],
        _turns=[],
        _turn_count=0,
        _write_session_file=lambda: None,
        _maybe_refresh_next_steps=lambda final: None,
    )


def run_app(build):
    """Build a ``QuestAITerminal`` with startup skipped, run ``build(app)`` once mounted."""
    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None  # skip the real (network) session build
        async with app.run_test() as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


# --- the busy indicator: peek-based count, never destructive ------------------------------

def test_peek_does_not_clear_the_inbox():
    inbox = InMemoryInbox()
    inbox.push("conv1", "hello")
    assert inbox.peek("conv1") == ["hello"]
    assert inbox.peek("conv1") == ["hello"]  # peek again: still there
    assert inbox.drain("conv1") == ["hello"]
    assert inbox.peek("conv1") == []


def test_busy_placeholder_shows_the_count_only_when_nonzero():
    assert "queued" not in _busy_placeholder(0)
    assert "(1 queued)" in _busy_placeholder(1)
    assert "(3 queued)" in _busy_placeholder(3)


def test_activity_bar_renders_queued_count_as_a_suffix():
    bar = ActivityBar()
    bar.set_status("Thinking…")
    bar.set_queued(2)
    rendered = bar.render().plain
    assert "Thinking…" in rendered
    assert "2 queued" in rendered
    bar.set_queued(0)
    assert "queued" not in bar.render().plain


# --- submitting a message mid-turn: queued, counted, never claimed as sent without being one ----

def test_message_typed_mid_turn_is_queued_and_counted():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._turn_active = True
        prompt = app.query_one("#prompt")
        prompt.text = "also check staging"
        prompt.post_message(PromptTextArea.Submitted(prompt, "also check staging"))
        await pilot.pause()
        return inbox.peek(app._session_id), app._activity._queued

    queued, shown_count = run_app(build)
    assert queued == ["also check staging"]
    assert shown_count == 1


def test_two_messages_typed_mid_turn_both_queue_and_count_increments():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._turn_active = True
        prompt = app.query_one("#prompt")
        for text in ("first thought", "second thought"):
            prompt.post_message(PromptTextArea.Submitted(prompt, text))
            await pilot.pause()
        return inbox.peek(app._session_id), app._activity._queued

    queued, shown_count = run_app(build)
    assert queued == ["first thought", "second thought"]
    assert shown_count == 2


# --- the core bug fix: a turn that ends must flush what's still queued, combined ---------------

def test_finish_turn_flushes_queued_messages_into_one_combined_new_turn():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "m1")
        inbox.push(app._session_id, "m2")

        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))

        # Mirrors an Escape cancel: the run stopped, nothing was answered.
        app._finish_turn("original request", None, 1.0, True, None)
        return started, inbox.peek(app._session_id)

    started, left_over = run_app(build)
    assert started == [("m1\n\nm2", {"echo": True, "auto": False})]
    assert left_over == []  # drained, not just read


def test_finish_turn_flushes_queued_messages_after_a_normal_completion_too():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "one more thing")

        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))

        final = OrchestratorResult(kind="answer", text="Here's the answer.")
        app._finish_turn("original request", final, 2.0, False, None)
        return started

    started = run_app(build)
    assert started == [("one more thing", {"echo": True, "auto": False})]


def test_finish_turn_does_not_start_a_new_turn_when_nothing_was_queued():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        app._finish_turn("original request", None, 1.0, True, None)
        return started

    assert run_app(build) == []


def test_finish_turn_never_auto_starts_a_turn_after_an_error_even_if_queued():
    """An errored turn must not immediately retry via whatever was queued -- that would turn one
    provider error into a runaway loop of auto-started turns."""
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "still there")
        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        app._finish_turn("original request", None, 1.0, False, RuntimeError("boom"))
        return started, inbox.peek(app._session_id)

    started, left_over = run_app(build)
    assert started == []
    assert left_over == ["still there"]  # not silently dropped either -- still there to flush later


def test_finish_turn_resets_the_queued_indicator():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._activity.set_queued(3)
        app._begin_turn = lambda text, **kw: None
        app._finish_turn("x", None, 1.0, True, None)
        return app._activity._queued

    assert run_app(build) == 0
