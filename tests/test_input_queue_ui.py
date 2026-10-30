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
from quest_ai_runner.textual_ui import ActivityBar, PromptTextArea, QuestAITerminal
from quest_ai_runner.textual_ui_widgets import _BUSY_PLACEHOLDER


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


# --- a note typed while the AI works is the person's own line, with no tag that could go stale -------

class RecordingLog:
    def __init__(self):
        self.lines = []
        self.styles = []

    def write(self, x):
        self.lines.append(getattr(x, "plain", str(x)))
        self.styles.append(getattr(x, "style", ""))

    def follow_now(self):
        self.lines.append("<follow>")


def test_a_note_typed_mid_turn_is_the_persons_own_line_with_no_tag_and_no_count():
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
    notes = [line for line in log.lines if line != "<follow>"]
    assert notes == ["❯ Also check the staging sheet.", "❯ And the notes."]
    assert log.styles[log.lines.index(notes[0])] == "bold cyan", "the style of the person's own message line"
    assert strip.endswith("· 2 messages waiting"), "the count is in the status strip only"
    assert not any("queued" in line or "sends next" in line or "waiting)" in line for line in log.lines), \
        "the log is append-only: a waiting tag would still be there after the note was picked up"
    assert log.lines[0] == "<follow>", "sending shows the newest output first"


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


def test_the_end_of_turn_flush_says_the_notes_are_worked_in_once_and_echoes_them_no_second_time():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        inbox.push(app._session_id, "m1")
        inbox.push(app._session_id, "m2")
        log = RecordingLog()
        app._tlog = log
        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        app._finish_turn("original request", None, 1.0, True, None)
        return log.lines, started

    lines, started = run_app(build)
    assert not [line for line in lines if "Sending" in line or "queued" in line or "(waiting)" in line]
    assert [line for line in lines if "Working your" in line] == ["  Working your 2 notes in now."]
    assert not any(line.startswith("❯") for line in lines), "the notes are already on screen as the person's own lines"
    assert [kw["echo"] for _text, kw in started] == [False]


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
    # Sent as one message, not echoed again (each note is already on screen as the person's own line), and put
    # back in the box if the AI call fails.
    assert started == [("m1\n\nm2", {"echo": False, "auto": False, "retry_text": "m1\n\nm2"})]
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
    assert started == [("one more thing", {"echo": False, "auto": False, "retry_text": "one more thing"})]


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


# --- a question at the end of the turn: notes typed before it was read are never its answer --------

def test_notes_are_not_sent_as_the_answer_to_a_question_the_person_has_not_read():
    """A note typed mid-run was written before the question was on screen. Sending it as the next message
    would make it the answer to something unread; it goes back in the box instead, nothing lost or ignored."""
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        log = RecordingLog()
        app._tlog = log
        started = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        inbox.push(app._session_id, "Also check the staging sheet.")
        inbox.push(app._session_id, "And skip anything that needs clearance.")
        final = OrchestratorResult(kind="confirm", question="Add them anyway, or skip them?",
                                   decision_id="dec_1")
        app._finish_turn("original request", final, 3.0, False, None)
        await pilot.pause()
        prompt = app.query_one("#prompt")
        return started, inbox.peek(app._session_id), prompt.text, log.lines

    started, left_in_inbox, box, lines = run_app(build)
    assert started == [], "no new turn: the notes are not an answer"
    assert left_in_inbox == [], "taken out of the inbox, so they are not sent a second time later"
    assert box == "Also check the staging sheet.\n\nAnd skip anything that needs clearance."
    assert "  Your 2 notes are back in the box. Send them with your answer or clear them." in lines
    question_at = next(i for i, line in enumerate(lines) if "Add them anyway" in line)
    back_at = next(i for i, line in enumerate(lines) if "back in the box" in line)
    assert question_at < back_at, "the person reads the question, then where their notes went"


def test_one_note_back_in_the_box_is_said_in_the_singular():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        log = RecordingLog()
        app._tlog = log
        app._begin_turn = lambda text, **kw: None
        inbox.push(app._session_id, "One thing.")
        app._finish_turn("x", OrchestratorResult(kind="confirm", question="Which?", decision_id="d"), 1.0, False, None)
        await pilot.pause()
        return log.lines

    assert "  Your note is back in the box. Send it with your answer or clear it." in run_app(build)


def test_notes_go_after_what_the_person_already_typed_in_the_box():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._tlog = RecordingLog()
        app._begin_turn = lambda text, **kw: None
        prompt = app.query_one("#prompt")
        prompt.text = "Skip them"
        inbox.push(app._session_id, "a note")
        app._finish_turn("x", OrchestratorResult(kind="confirm", question="Which?", decision_id="d"), 1.0, False, None)
        await pilot.pause()
        return prompt.text

    assert run_app(build) == "Skip them\n\na note"


def test_a_question_raised_mid_run_still_holds_the_notes_back_when_the_result_carries_no_decision():
    """The question can reach the terminal as an event only (``pending_question``); that is as much a question."""
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._tlog = RecordingLog()
        started = []
        app._begin_turn = lambda text, **kw: started.append(text)
        app.pending_question = "Add them anyway, or skip them?"
        inbox.push(app._session_id, "a note")
        app._finish_turn("x", OrchestratorResult(kind="answer", text="Here is the plan."), 1.0, False, None)
        await pilot.pause()
        return started, app.query_one("#prompt").text

    started, box = run_app(build)
    assert started == [] and box == "a note"


def test_a_cancelled_turn_with_an_unwritten_question_flushes_the_notes_and_asks_nothing_of_the_person():
    """Esc after a question event: the question was never written, so there is nothing to answer. The notes are
    sent as the next message, and the prompt does not invite an answer to a question that is not on screen."""
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = _stub_session(inbox)
        app._tlog = RecordingLog()
        started = []
        app._begin_turn = lambda text, **kw: started.append(text)
        app._awaiting_decision = True
        app.pending_question = "Add them anyway, or skip them?"
        inbox.push(app._session_id, "a note")
        app._finish_turn("x", None, 1.0, True, None)
        await pilot.pause()
        return started, app._awaiting_decision, app.query_one("#prompt").placeholder

    started, awaiting, placeholder = run_app(build)
    assert started == ["a note"] and awaiting is False
    assert "question above" not in placeholder


def test_a_decision_event_mid_run_says_the_ai_is_still_working_and_the_question_comes_at_the_step_end():
    async def build(app, pilot):
        app.sess = _stub_session(InMemoryInbox())
        app._activity.display = True
        app._handle_event({"type": "decision", "text": "Add them anyway, or skip them?"})
        return app._activity._text, app.query_one("#prompt").placeholder

    status, placeholder = run_app(build)
    assert status == "Working… I'll have a question for you when this step ends."
    assert "Waiting for your answer" not in status
    assert "question above" not in placeholder, "no question is on screen yet"
