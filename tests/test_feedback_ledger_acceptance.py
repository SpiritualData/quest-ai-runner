"""The additions that make the ledger safe for asks that come from PEOPLE, not from a lane.

Separate from ``test_feedback_ledger.py`` (which covers the record as it stands) because these
cover one question: what stops an assistant from closing a person's request on its own say-so, and
what stops something captured automatically from being read as an instruction.
"""
import json
import multiprocessing as mp
from pathlib import Path

from quest_ai_runner.runner.feedback_ledger import (
    ACCEPTED, AWAITING_ACCEPTANCE, AWAITING_ANSWER, BLOCKED, DONE, IN_FORCE, KIND_QUESTION,
    KIND_REQUEST, KIND_STANDING, KIND_UNKNOWN, OPEN, OPEN_STATES, FeedbackLedger,
    build_ledger, record_run_account,
)


def ledger(tmp_path, **kw) -> FeedbackLedger:
    return FeedbackLedger(str(tmp_path / "state_feedback.json"), **kw)


def an_item(led, item_id="n1", text="please add the Status column"):
    led.observe(card_id="q1", source="notes", item_id=item_id, text=text, author="joshua")
    return item_id


# --- a run's claim is not a person's acceptance -----------------------------------------------

def test_without_the_gate_a_run_still_finishes_things(tmp_path):
    """The default is exactly what it was: a lane's own asks close on the run's account."""
    led = ledger(tmp_path)
    an_item(led)
    item = led.apply_disposition(card_id="q1", source="notes", item_id="n1", disposition="done")
    assert item.state == DONE
    assert not item.is_open


def test_with_the_gate_done_only_reaches_awaiting_acceptance(tmp_path):
    led = ledger(tmp_path, requires_acceptance=True)
    an_item(led)
    item = led.apply_disposition(card_id="q1", source="notes", item_id="n1",
                                 disposition="done", note="column added",
                                 evidence=["https://example.invalid/sheet#gid=0"], run_id="run-7")
    assert item.state == AWAITING_ACCEPTANCE
    assert item.is_open, "a claim nobody has checked is still owed"
    assert item.evidence == ["https://example.invalid/sheet#gid=0"]
    assert item.last_run_id == "run-7"
    assert led.open_items("q1") and led.open_items("q1")[0].item_id == "n1"


def test_only_a_person_accepts_and_a_run_cannot_undo_it(tmp_path):
    led = ledger(tmp_path, requires_acceptance=True)
    an_item(led)
    led.apply_disposition(card_id="q1", source="notes", item_id="n1", disposition="done")
    assert led.accept(card_id="q1", source="notes", item_id="n1", by="run") is None, \
        "a run may not accept its own work"
    accepted = led.accept(card_id="q1", source="notes", item_id="n1", note="looks right")
    assert accepted.state == ACCEPTED and accepted.set_by_person
    assert not accepted.is_open
    again = led.apply_disposition(card_id="q1", source="notes", item_id="n1",
                                  disposition="partial", note="actually reopening this")
    assert again.state == ACCEPTED, "a person's acceptance outranks any later run"


# --- a question is not a request, and an untriaged capture is not an instruction ---------------

def test_asked_puts_it_on_the_person_and_authorises_nothing(tmp_path):
    led = ledger(tmp_path)
    an_item(led, text="send this to the whole list?")
    item = led.apply_disposition(card_id="q1", source="notes", item_id="n1",
                                 disposition="asked", note="asked whether to include partners")
    assert (item.kind, item.state) == (KIND_QUESTION, AWAITING_ANSWER)
    assert item.is_open and AWAITING_ANSWER in OPEN_STATES
    assert item.authorizes_execution is False


def test_blocked_is_owed_and_keeps_the_kind(tmp_path):
    led = ledger(tmp_path)
    an_item(led, text="from now on include the page numbers")
    led.apply_disposition(card_id="q1", source="notes", item_id="n1", disposition="standing")
    item = led.apply_disposition(card_id="q1", source="notes", item_id="n1",
                                 disposition="blocked", note="no access to the sheet")
    assert item.state == BLOCKED and item.kind == KIND_STANDING
    assert item.is_open and "no access" in item.status_line()


def test_an_untriaged_capture_authorises_nothing_until_a_person_classifies_it(tmp_path):
    """What an automatic mailbox sweep creates, and what it may cause: nothing, until triage."""
    led = ledger(tmp_path, requires_acceptance=True)
    led.observe(card_id="inbox", source="email", item_id="msg-1",
                text="can you look at the invoice", author="someone@example.invalid")
    captured = led.get("inbox", "email", "msg-1")
    assert (captured.kind, captured.state) == (KIND_UNKNOWN, OPEN)
    assert captured.authorizes_execution is False, "capture is a candidate, never an instruction"
    triaged = led.set_state(card_id="inbox", source="email", item_id="msg-1",
                            state=OPEN, kind=KIND_REQUEST, note="yes, do this", by="person")
    assert triaged.kind == KIND_REQUEST and triaged.set_by_person
    assert triaged.authorizes_execution is True


def test_all_items_reads_the_whole_store(tmp_path):
    led = ledger(tmp_path)
    an_item(led, item_id="n1")
    led.observe(card_id="q2", source="email", item_id="msg-9", text="another one")
    assert {i.item_id for i in led.all_items()} == {"n1", "msg-9"}


# --- one file, two processes ------------------------------------------------------------------

def write_one(path: str, index: int) -> None:
    led = FeedbackLedger(path)
    led.observe(card_id="q1", source="notes", item_id=f"n{index}", text=f"ask {index}")


def test_two_processes_writing_the_same_ledger_keep_both_rows(tmp_path):
    """The failure this guards: a poller and a consumer backend overwriting each other silently."""
    path = str(tmp_path / "shared_feedback.json")
    procs = [mp.Process(target=write_one, args=(path, i)) for i in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    rows = json.loads(Path(path).read_text())["items"]
    assert len(rows) == 8, f"rows were lost between processes: {sorted(rows)}"


def test_a_read_sees_what_another_writer_wrote(tmp_path):
    path = str(tmp_path / "shared_feedback.json")
    reader = FeedbackLedger(path, read_only=True)
    assert reader.all_items() == []
    FeedbackLedger(path).observe(card_id="q1", source="notes", item_id="n1", text="later")
    assert [i.item_id for i in reader.all_items()] == ["n1"], "a stale reader is a wrong reader"


# --- the mixed note keeps its one-off out of guidance ------------------------------------------

def test_a_mixed_note_cards_only_the_standing_half(tmp_path):
    led = ledger(tmp_path)
    paragraph = ("Add another column 'Status'. From now on report what you are doing on "
                 "limitations gathering in every report.")
    led.observe(card_id="q1", source="notes", item_id="n1", text=paragraph, author="joshua")
    carded = {}

    def writer(*, text, item):
        carded["text"] = text
        return "card-1"

    record_run_account(
        led, card_id="q1", offered=[("U1", "notes", "n1")],
        dispositions={"U1": [("done", "added the Status column"),
                             ("standing", "report limitations work every time")]},
        guidance_writer=writer)
    assert carded["text"] == "report limitations work every time"
    assert "Status column" not in carded["text"], "a one-off must never become policy"
    assert led.get("q1", "notes", "n1").text == paragraph, "their original words are preserved"


def test_a_mixed_note_with_no_focused_words_cards_nothing(tmp_path):
    led = ledger(tmp_path)
    led.observe(card_id="q1", source="notes", item_id="n1",
                text="Add a Status column. And always include page numbers.", author="joshua")
    calls = []
    record_run_account(
        led, card_id="q1", offered=[("U1", "notes", "n1")],
        dispositions={"U1": [("done", "added it"), ("standing", "")]},
        guidance_writer=lambda *, text, item: calls.append(text) or "card-1")
    assert calls == [], "with nothing focused to card, the paragraph must not be carded"
    assert led.get("q1", "notes", "n1#2").state == IN_FORCE, "it is still tracked as standing"


# --- config plumbing ---------------------------------------------------------------------------

def test_build_ledger_carries_the_acceptance_gate(tmp_path):
    class Cfg:
        feedback_ledger = True
        feedback_ledger_path = str(tmp_path / "x_feedback.json")

    assert build_ledger(Cfg()).requires_acceptance is False
    assert build_ledger(Cfg(), requires_acceptance=True).requires_acceptance is True
