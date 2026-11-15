"""A note typed during a run that the worker took but never confirmed reaches the next attempt, and is shown as taken up (so no extra run starts for it)."""
from types import SimpleNamespace

from quest_ai_runner.the_consciousness.terminal.opencode_terminal import Turn
from quest_ai_runner.the_consciousness.terminal.textual_ui_control import TerminalRunControl
from quest_ai_runner.the_intelligence.mind.delegation.inbox import InMemoryInbox


def make_turn():
    inbox = InMemoryInbox()
    turn = Turn.__new__(Turn)
    turn.harness = SimpleNamespace(inbox_key="k", sent={})
    turn.session = SimpleNamespace(_orch=SimpleNamespace(input_inbox=inbox))
    turn.control = TerminalRunControl(inbox, "k", cancel_check=lambda: False)
    seen = []
    turn.taken_up = seen.extend
    return turn, inbox, seen


def test_a_note_the_worker_took_but_never_confirmed_goes_to_the_next_attempt_once():
    turn, inbox, seen = make_turn()
    inbox.push("k", "use the second device")
    assert [m.text for m in turn.control.claim_inputs()] == ["use the second device"]       # the worker polled it at the end of its attempt
    inbox.push("k", "and skip the build")                                                  # typed while the goal was being checked
    assert turn.pending_notes() == ["use the second device", "and skip the build"]
    assert seen == ["use the second device", "and skip the build"]
    assert turn.pending_notes() == []


def test_an_interface_with_only_an_inbox_gets_a_control_that_carries_unconfirmed_notes():
    from quest_ai_runner.the_intelligence.mind.delegation.inbox_control import PendingNotes
    inbox = InMemoryInbox()
    notes = PendingNotes(lambda: inbox.drain("c"))
    control = notes.run_control()
    inbox.push("c", "first")
    got = control.claim_inputs()
    assert [m.text for m in got] == ["first"]
    inbox.push("c", "second")
    assert notes.between_attempts() == ["first", "second"]          # the worker never confirmed "first"
    inbox.push("c", "third")
    got = control.claim_inputs()
    control.mark_seen([m.id for m in got], "deep_worker")
    assert notes.between_attempts() == []


def test_a_goal_not_met_always_says_why_in_one_short_sentence():
    from quest_ai_runner.the_consciousness.terminal.opencode_terminal import Turn
    worker = SimpleNamespace(stalled="", error="Goal not yet met: the build never produced an installable app. More detail follows.")
    assert Turn.unmet_reason(SimpleNamespace(deep_results=[worker]), "stopped") == "the build never produced an installable app"
    assert Turn.unmet_reason(SimpleNamespace(deep_results=[], goal_verdict={"met": False, "reason": "no files were changed"}), "unmet") == "no files were changed"
    assert Turn.unmet_reason(SimpleNamespace(deep_results=[]), "stopped")           # even with nothing reported, a reason is said
