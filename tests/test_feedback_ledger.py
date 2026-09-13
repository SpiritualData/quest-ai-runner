"""What a person asked for, and how far anybody actually got with it.

The ask that produced ``runner/feedback_ledger.py``: replying is not doing. Every channel could
say what had arrived and none could say what became of it, so "handled" was inferred from
circumstance (a reply under a comment, a run finishing later the same afternoon) and a standing
instruction was closed the first day it was honoured.

What this file pins down:

  * A STATUS IS RECORDED, NEVER INFERRED. A run's own declared disposition moves an item, and a
    disposition this library does not know moves nothing at all.
  * A STANDING RULE IS NEVER DONE. It stays in force and carries how far it has got, because
    marking it done the day you follow it is exactly how it stops being followed.
  * A PERSON OUTRANKS A RUN. A status a human set cannot be overwritten by any run.
  * ONE PARAGRAPH CAN HOLD TWO ASKS, and a record that can only hold one verdict for the pair has
    to lose one of them.

Offline, driven against fakes and a tmp_path store.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.context_updates import (
    UpdateEngine,
    parse_dispositions,
    split_disposition,
    usage_receipt_gate,
)
from quest_ai_runner.runner.feedback_ledger import (
    DONE,
    IN_FORCE,
    IN_PROGRESS,
    KIND_REQUEST,
    KIND_STANDING,
    NEEDS_REAPPLYING,
    OPEN,
    PARTIALLY_APPLIED,
    FeedbackLedger,
    ask_id,
    build_ledger,
    guidance_writer_for,
    ledger_path_for,
    normalize_disposition,
    open_items_block,
    record_run_account,
)

NOW = datetime(2026, 9, 12, 9, 0, 0, tzinfo=timezone.utc)

# Joshua's real note on the dissertation quest, 2026-09-11, trimmed. One paragraph, two asks: a
# column to add now, and a rule about every future report.
REAL_NOTE = (
    'Fix that that add another column "Proposed solution" and another "Status" with statuses '
    "Needs Review, Incorporated, Acknowledged, Ignored. From now one I want as part of your "
    "report what you're doing on 1. limitations gathering and solutioning and 2. case gathering "
    "and preparation."
)


def _ledger(tmp_path=None):
    return FeedbackLedger(str(tmp_path / "ledger.json") if tmp_path else None)


def _observe(ledger, item_id="note_1", text=REAL_NOTE, source="quest_notes"):
    return ledger.observe(card_id="q1", source=source, item_id=item_id, text=text,
                          author="the owner", occurred_at=NOW - timedelta(days=1), at=NOW)


# --- the vocabulary ----------------------------------------------------------------------------

def test_only_a_listed_disposition_means_anything():
    assert normalize_disposition("done") == "done"
    assert normalize_disposition("  DONE ") == "done"
    assert normalize_disposition("standing rule") == "standing"
    assert normalize_disposition("partially applied") == "standing-partial"
    # Prose is not a disposition. This is the whole safety property: a run that writes an essay
    # instead of choosing moves nothing, rather than moving something by accident.
    assert normalize_disposition("I took care of that for you") == ""
    assert normalize_disposition("") == ""


def test_a_receipt_line_can_carry_two_asks():
    pairs = split_disposition("done: added the Status column; standing: report limitations work")
    assert pairs == [("done", "added the Status column"),
                     ("standing", "report limitations work")]


def test_a_line_with_no_disposition_records_nothing():
    assert split_disposition("cited in the method section") == []
    assert parse_dispositions("Context used:\n  [U1] cited in the method section\n") == {}


def test_the_run_is_shown_the_whole_vocabulary_it_must_choose_from():
    """A run cannot pick from a list it was never given, and a guessed word records nothing."""
    gate = usage_receipt_gate(["U1"])
    for name in ("done", "partial", "standing", "standing-partial", "declined", "noted"):
        assert name in gate
    assert "standing, never done" in gate


# --- what a run's account does to the record ----------------------------------------------------

def test_seeing_something_is_not_acting_on_it(tmp_path):
    ledger = _ledger(tmp_path)
    item = _observe(ledger)
    assert item.state == OPEN
    assert item.applications == 0

    # Offered again the next day, still nobody has done anything about it.
    ledger.observe(card_id="q1", source="quest_notes", item_id="note_1", at=NOW + timedelta(days=1))
    assert ledger.get("q1", "quest_notes", "note_1").state == OPEN


def test_a_standing_rule_is_never_done_however_the_run_words_it(tmp_path):
    """The failure this whole module exists for.

    A rule followed on Tuesday is not a rule discharged. Recorded as done, nothing raises it again,
    and Thursday's report drops it with nobody the wiser.
    """
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="standing", note="reported limitations work", at=NOW)

    item = ledger.get("q1", "quest_notes", "note_1")
    assert item.kind == KIND_STANDING
    assert item.state == IN_FORCE
    assert item.state != DONE

    # A later run writes "done" on the day it complies. It must not demote the rule to a request.
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="done", note="included again", at=NOW + timedelta(days=2))
    item = ledger.get("q1", "quest_notes", "note_1")
    assert item.kind == KIND_STANDING
    assert item.state == IN_FORCE
    assert item.applications == 2


def test_a_rule_followed_only_in_part_says_so(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                            disposition="standing-partial",
                            note="limitations yes, case gathering not yet", at=NOW)

    item = ledger.get("q1", "quest_notes", "note_1")
    assert item.state == PARTIALLY_APPLIED
    assert item.is_open, "a half-applied rule is still owed something"
    assert "applied only in part" in item.status_line()
    assert "case gathering not yet" in item.status_line()


def test_a_request_finishes_and_stops_being_owed(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger, item_id="note_2", text="add a Status column to the sheet")
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_2",
                             disposition="partial", note="column added, statuses pending", at=NOW)
    assert ledger.get("q1", "quest_notes", "note_2").state == IN_PROGRESS
    assert ledger.get("q1", "quest_notes", "note_2").is_open

    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_2",
                             disposition="done", note="statuses in", at=NOW + timedelta(hours=2))
    item = ledger.get("q1", "quest_notes", "note_2")
    assert item.state == DONE
    assert item.kind == KIND_REQUEST
    assert not item.is_open


def test_an_unknown_disposition_leaves_the_item_exactly_as_it_was(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger)
    assert ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                                    disposition="handled it", note="", at=NOW) is None
    assert ledger.get("q1", "quest_notes", "note_1").state == OPEN


def test_a_person_outranks_a_run(tmp_path):
    """Otherwise the ledger is the assistant marking its own homework."""
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.set_state(card_id="q1", source="quest_notes", item_id="note_1", state=OPEN,
                     note="no, this is still not done", by="person", at=NOW)

    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="done", note="I think I did it", at=NOW)
    item = ledger.get("q1", "quest_notes", "note_1")
    assert item.state == OPEN
    assert item.set_by_person

    # Until the person themselves moves it.
    ledger.set_state(card_id="q1", source="quest_notes", item_id="note_1", state=DONE,
                     by="person", at=NOW)
    assert ledger.get("q1", "quest_notes", "note_1").state == DONE


def test_coming_back_to_a_standing_rule_means_it_needs_reapplying(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="standing", at=NOW)
    ledger.reopen(card_id="q1", source="quest_notes", item_id="note_1",
                  note="you dropped it again", at=NOW + timedelta(days=3))

    item = ledger.get("q1", "quest_notes", "note_1")
    assert item.state == NEEDS_REAPPLYING
    assert item.is_open
    assert item.kind == KIND_STANDING, "reopening does not turn a rule back into a one-off"


def test_the_record_survives_a_restart(tmp_path):
    """A record of what is owed that forgets on restart is worse than none: it reads as complete."""
    path = str(tmp_path / "ledger.json")
    first = FeedbackLedger(path)
    first.observe(card_id="q1", source="quest_notes", item_id="note_1", text=REAL_NOTE, at=NOW)
    first.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                            disposition="standing-partial", note="half of it", at=NOW)

    reloaded = FeedbackLedger(path)
    item = reloaded.get("q1", "quest_notes", "note_1")
    assert item is not None
    assert item.state == PARTIALLY_APPLIED
    assert item.kind == KIND_STANDING
    assert item.text == REAL_NOTE


# --- one run's whole account --------------------------------------------------------------------

def test_one_paragraph_two_asks_becomes_two_tracked_things(tmp_path):
    """Joshua's real note: a column to add now, and a rule for every future report.

    One verdict for the pair has to lose one of them. Mark it done and the standing rule stops
    existing; mark it standing and the column never gets built.
    """
    ledger = _ledger(tmp_path)
    _observe(ledger)

    moved = record_run_account(
        ledger, card_id="q1",
        offered=[("U1", "quest_notes", "note_1")],
        dispositions=parse_dispositions(
            "Context used:\n"
            "  [U1] done: added the Status column; standing: report limitations and case work\n"),
        at=NOW)

    assert len(moved) == 2
    request = ledger.get("q1", "quest_notes", "note_1")
    rule = ledger.get("q1", "quest_notes", ask_id("note_1", 2))
    assert request.state == DONE and request.kind == KIND_REQUEST
    assert rule.state == IN_FORCE and rule.kind == KIND_STANDING
    assert rule.text == REAL_NOTE, "the second ask keeps the words it came from"
    # And only the half that is a rule is still owed anything.
    assert [i.item_id for i in ledger.open_items("q1")] == []
    assert [i.item_id for i in ledger.standing_rules("q1")] == ["note_1#2"]


def test_a_ref_the_run_said_nothing_about_moves_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger)
    moved = record_run_account(ledger, card_id="q1",
                               offered=[("U1", "quest_notes", "note_1")],
                               dispositions={}, at=NOW)
    assert moved == []
    assert ledger.get("q1", "quest_notes", "note_1").state == OPEN


# --- the guidance bridge ------------------------------------------------------------------------

class _Cards:
    """A ``GuidanceCardManager``-shaped store."""

    def __init__(self):
        self.saved = []

    def save_card(self, *, card_id, title, body, description="", tags=()):
        self.saved.append({"card_id": card_id, "title": title, "body": body,
                           "description": description, "tags": list(tags)})
        return card_id


def test_a_standing_rule_becomes_guidance_in_the_persons_own_words(tmp_path):
    """The merge, and the condition on it: only what is STANDING earns a card.

    The ledger says whether a rule is being followed; the card is what puts it in front of
    tomorrow's run. Neither can do the other's job.
    """
    ledger = _ledger(tmp_path)
    cards = _Cards()
    _observe(ledger)

    record_run_account(
        ledger, card_id="q1",
        offered=[("U1", "quest_notes", "note_1")],
        dispositions=parse_dispositions(
            "Context used:\n  [U1] standing: reported limitations work this time\n"),
        at=NOW, guidance_writer=guidance_writer_for(cards))

    assert len(cards.saved) == 1
    card = cards.saved[0]
    assert "limitations gathering" in card["body"], "their words, not a paraphrase"
    assert "standing instruction, not a" in card["body"]
    assert ledger.get("q1", "quest_notes", "note_1").guidance_card_id == card["card_id"]


def test_a_one_time_request_never_becomes_a_rule(tmp_path):
    """The condition Joshua put on merging the two: a one-off must not become a standing rule.

    A card saying "add a Status column" would be retrieved into every future run forever, as
    though it were policy.
    """
    ledger = _ledger(tmp_path)
    cards = _Cards()
    _observe(ledger, item_id="note_2", text="add a Status column to the sheet")

    record_run_account(ledger, card_id="q1",
                       offered=[("U1", "quest_notes", "note_2")],
                       dispositions=parse_dispositions("Context used:\n  [U1] done: added it\n"),
                       at=NOW, guidance_writer=guidance_writer_for(cards))

    assert cards.saved == []


def test_a_rule_is_carded_once_however_often_it_is_applied(tmp_path):
    ledger = _ledger(tmp_path)
    cards = _Cards()
    _observe(ledger)
    for day in (0, 1, 2):
        record_run_account(
            ledger, card_id="q1", offered=[("U1", "quest_notes", "note_1")],
            dispositions=parse_dispositions("Context used:\n  [U1] standing: followed it\n"),
            at=NOW + timedelta(days=day), guidance_writer=guidance_writer_for(cards))
    assert len(cards.saved) == 1
    assert ledger.get("q1", "quest_notes", "note_1").applications == 3


def test_a_broken_guidance_store_never_costs_the_record(tmp_path):
    class _Broken:
        def save_card(self, **kwargs):
            raise RuntimeError("no disk")

    ledger = _ledger(tmp_path)
    _observe(ledger)
    moved = record_run_account(
        ledger, card_id="q1", offered=[("U1", "quest_notes", "note_1")],
        dispositions=parse_dispositions("Context used:\n  [U1] standing: followed it\n"),
        at=NOW, guidance_writer=guidance_writer_for(_Broken()))
    assert moved and moved[0].state == IN_FORCE
    assert moved[0].guidance_card_id == ""


# --- what the next run is shown -----------------------------------------------------------------

def test_what_is_still_owed_is_offered_to_the_next_run_with_its_state(tmp_path):
    """The point of all of it: a later run inherits the backlog, with how far each thing got."""
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="standing-partial",
                             note="limitations yes, cases not yet", at=NOW)

    class _NoNews:
        def list_quest_notes(self, quest_id):
            return []

    bundle = UpdateEngine(_NoNews(), ledger=ledger, always=("quest_notes",),
                          now_fn=lambda: NOW + timedelta(days=1)).collect(
        {"quest_id": "q1", "name": "Dissertation"}, card_id="q1")

    owed = [u for u in bundle.updates if u.kind == "still owed"]
    assert len(owed) == 1
    assert owed[0].ref, "it must carry a ref, or nothing can ever close it"
    assert owed[0].needs_response
    assert "applied only in part" in owed[0].title
    assert "limitations gathering" in owed[0].body


def test_a_closed_item_is_not_dragged_into_the_next_run(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger, item_id="note_2", text="add a Status column")
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_2",
                             disposition="done", at=NOW)

    class _NoNews:
        def list_quest_notes(self, quest_id):
            return []

    bundle = UpdateEngine(_NoNews(), ledger=ledger, always=("quest_notes",),
                          now_fn=lambda: NOW + timedelta(days=1)).collect(
        {"quest_id": "q1"}, card_id="q1")
    assert [u for u in bundle.updates if u.kind == "still owed"] == []


def test_a_rule_in_force_is_not_re_announced_as_news(tmp_path):
    """A rule that is being followed belongs in guidance, not in tomorrow's headlines."""
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="standing", at=NOW)

    class _NoNews:
        def list_quest_notes(self, quest_id):
            return []

    bundle = UpdateEngine(_NoNews(), ledger=ledger, always=("quest_notes",),
                          now_fn=lambda: NOW + timedelta(days=1)).collect(
        {"quest_id": "q1"}, card_id="q1")
    assert [u for u in bundle.updates if u.kind == "still owed"] == []
    assert ledger.standing_rules("q1"), "it is still tracked, just not re-announced"


def test_a_recorded_status_outranks_the_timestamp_reading(tmp_path):
    """A run finishing after a note is circumstance. What the run SAID is evidence."""
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="partial", note="started", at=NOW)

    class _NoteAndLaterRun:
        def list_quest_notes(self, quest_id):
            return [{"id": "note_1", "text": REAL_NOTE, "author_kind": "user",
                     "author_name": "the owner",
                     "created_at": (NOW - timedelta(days=1)).isoformat()}]

        def list_tasks(self, **kwargs):
            # A run delivered a result AFTER the note: under the old rule that closed it.
            return [{"id": "t1", "status": "done", "result": "the day's brief",
                     "worked_at": NOW.isoformat()}]

    bundle = UpdateEngine(_NoteAndLaterRun(), ledger=ledger, always=("quest_notes",),
                          now_fn=lambda: NOW + timedelta(days=1)).collect(
        {"quest_id": "q1"}, card_id="q1")

    notes = [u for u in bundle.updates if u.source == "quest_notes"]
    assert len(notes) == 1
    assert notes[0].needs_response, "the run said it only started; a later result does not finish it"
    assert "started, not finished" in notes[0].title


def test_the_owed_block_reads_as_a_backlog_not_as_news(tmp_path):
    ledger = _ledger(tmp_path)
    _observe(ledger)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                             disposition="standing-partial", note="cases not yet", at=NOW)
    block = open_items_block(ledger.open_items("q1"))
    assert "STILL OWED" in block
    assert "not new" in block
    assert "limitations gathering" in block


# --- wiring ---------------------------------------------------------------------------------

def test_the_record_lives_beside_the_lanes_own_state():
    assert ledger_path_for(None, "/x/qar_state.json") == "/x/qar_state_feedback.json"
    assert ledger_path_for("/somewhere/else.json", "/x/qar_state.json") == "/somewhere/else.json"
    assert ledger_path_for(None, None) is None


def test_a_deployment_can_switch_it_off():
    class Off:
        feedback_ledger = False
    assert build_ledger(Off(), state_path="/x/qar_state.json") is None

    class On:
        feedback_ledger = True
        feedback_ledger_path = None
    assert build_ledger(On(), state_path="/x/qar_state.json") is not None


def test_a_read_only_ledger_never_writes(tmp_path):
    """``quest-ai-runner context`` inspects the record; inspecting must not change it."""
    path = tmp_path / "ledger.json"
    FeedbackLedger(str(path)).observe(card_id="q1", source="quest_notes", item_id="note_1",
                                      text="x", at=NOW)
    before = path.read_text()

    ro = FeedbackLedger(str(path), read_only=True)
    ro.observe(card_id="q1", source="quest_notes", item_id="note_2", text="y", at=NOW)
    ro.apply_disposition(card_id="q1", source="quest_notes", item_id="note_1",
                         disposition="done", at=NOW)
    assert path.read_text() == before


def test_a_persons_own_diary_is_never_something_they_owe_an_answer_to(tmp_path):
    """Live failure, first run of this: the habit log and the daily reflection were tracked as
    asks, so the next morning both came back as "still owed, needs an answer" -- an assistant
    asking a person to answer their own diary."""
    from quest_ai_runner.runner.context_updates import Watermarks

    class _Client:
        def list_quest_notes(self, quest_id):
            return [{"id": "n1", "text": "please add the page numbers", "author_kind": "user",
                     "created_at": (NOW - timedelta(days=1)).isoformat()}]

        def get_period_reflection(self, **kwargs):
            return {"period": "week", "text": "a good week", "date": NOW.date().isoformat()}

    ledger = _ledger(tmp_path)
    bundle = UpdateEngine(_Client(), ledger=ledger, watermarks=Watermarks(None),
                          now_fn=lambda: NOW).collect({"quest_id": "q1"}, card_id="q1")
    bundle.mark_seen()

    tracked = {i.source for i in ledger.for_card("q1")}
    assert tracked == {"quest_notes"}, f"only asks are tracked, got {tracked}"
    assert not [i for i in ledger.for_card("q1") if i.source == "reflections"]
