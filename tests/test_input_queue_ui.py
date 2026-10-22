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
from quest_ai_runner.textual_ui import ActivityBar, PromptTextArea, QuestAITerminal, _BUSY_PLACEHOLDER


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


def test_the_busy_placeholder_says_what_typing_does_and_how_to_stop_and_carries_no_count():
    assert _BUSY_PLACEHOLDER == "Type to add a note for the AI. Esc stops the work."
    assert "queue" not in _BUSY_PLACEHOLDER and not any(ch.isdigit() for ch in _BUSY_PLACEHOLDER)


def test_activity_bar_renders_queued_count_as_a_suffix():
    bar = ActivityBar()
    bar.set_status("Thinking…")
    bar.set_queued(2)
    rendered = bar.render().plain
    assert "Thinking…" in rendered
    assert "2 messages waiting" in rendered
    assert "—" not in rendered, "no em dash on screen"
    bar.set_queued(1)
    assert "1 message waiting" in bar.render().plain
    bar.set_queued(0)
    assert "waiting" not in bar.render().plain


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


# --- a note typed while the AI works is the person's own line, tagged waiting ----------------------

class RecordingLog:
    def __init__(self):
        self.lines = []
        self.styles = []

    def write(self, x):
        self.lines.append(getattr(x, "plain", str(x)))
        self.styles.append(getattr(x, "style", ""))


def test_a_note_typed_mid_turn_is_the_persons_own_line_with_a_waiting_tag_and_no_count():
    async def build(app, pilot):
        app.sess = _stub_session(InMemoryInbox())
        app._turn_active = True
        log = RecordingLog()
        app._tlog = log
        prompt = app.query_one("#prompt")
        for text in ("Also check the staging sheet.", "And the notes."):
            prompt.post_message(PromptTextArea.Submitted(prompt, text))
            await pilot.pause()
        return log, app._activity.render().plain, prompt.placeholder

    log, strip, placeholder = run_app(build)
    assert log.lines == ["❯ Also check the staging sheet.  (waiting)", "❯ And the notes.  (waiting)"]
    assert log.styles[0] == "bold cyan", "the style of the person's own message line"
    assert strip.endswith("· 2 messages waiting"), "the count is in the status strip only"
    assert not any("queued" in line or "sends next" in line for line in log.lines)


def test_the_pickup_of_notes_is_one_plain_dim_line_without_a_stale_count():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        log = RecordingLog()
        app._tlog = log
        for text in ("a", "b"):
            inbox.push(app._session_id, text)
        pending = app._make_pending_drain(inbox, app._session_id)
        await asyncio.to_thread(pending)
        await pilot.pause()
        inbox.push(app._session_id, "c")
        await asyncio.to_thread(pending)
        await pilot.pause()
        return log

    log = run_app(build)
    assert log.lines == ["  Working your 2 notes in now.", "  Working your note in now."]
    assert set(log.styles) == {"dim"}


def test_the_end_of_turn_flush_sends_the_notes_without_a_sending_line_of_its_own():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "m1")
        log = RecordingLog()
        app._tlog = log
        app._begin_turn = lambda text, **kw: None
        app._finish_turn("original request", None, 1.0, True, None)
        return log.lines

    assert not [line for line in run_app(build) if "Sending" in line or "queued" in line]


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


# --- the mid-run drain must be VISIBLE too, not just the end-of-turn flush ---------------------

def test_mid_run_drain_reports_the_pickup_instead_of_leaving_queued_stale():
    """Task-reported bug: when the orchestrator's OWN loop (not ``_finish_turn``) drains a queued
    message mid-run to fold it into a retry, the "queued" line written at queue-time used to sit in
    the transcript forever with no sign the message was ever picked up -- only the orchestrator's
    own loop knew. ``_make_pending_drain`` wraps the raw ``inbox.drain`` the orchestrator calls so
    the UI finds out too.

    Production calls ``pending_inputs`` from the worker thread running the orchestrator stream
    (``_run_stream``, ``@work(thread=True)``), never the UI thread -- ``call_from_thread`` requires
    exactly that, so the test mirrors it with ``asyncio.to_thread``.
    """
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "one more thing")

        pending = app._make_pending_drain(inbox, app._session_id)
        drained = await asyncio.to_thread(pending)  # mirrors the orchestrator's own worker thread
        await pilot.pause()
        return drained, app._activity._queued

    drained, shown_count = run_app(build)
    assert drained == ["one more thing"]
    assert shown_count == 0  # nothing left outstanding after the pickup


def test_mid_run_drain_is_silent_when_nothing_was_queued():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        pending = app._make_pending_drain(inbox, app._session_id)
        return await asyncio.to_thread(pending)

    assert run_app(build) == []


def test_finish_turn_resets_the_queued_indicator():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._activity.set_queued(3)
        app._begin_turn = lambda text, **kw: None
        app._finish_turn("x", None, 1.0, True, None)
        return app._activity._queued

    assert run_app(build) == 0
