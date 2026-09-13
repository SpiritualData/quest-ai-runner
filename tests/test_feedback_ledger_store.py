"""The pluggable ``FeedbackStore`` seam on ``FeedbackLedger``.

Added for a second consumer whose durable record is not a file: a live, in-process product
surface with its own database. The seam has to prove three things a file-backed ledger gets for
free: a store round-trips the same items a file would, two ``FeedbackLedger`` instances sharing
one store see each other's writes (the multi-process case a real deployment needs), and a broken
store degrades exactly like a missing file does -- empty on read, logged and swallowed on write --
rather than raising into a caller that never expected a ledger call to fail.

Offline; the store here is an in-memory dict standing in for a real database.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.feedback_ledger import (
    ACCEPTED,
    AWAITING_ACCEPTANCE,
    DONE,
    FeedbackLedger,
)

NOW = datetime(2026, 9, 12, 9, 0, 0, tzinfo=timezone.utc)


class DictStore:
    """The simplest thing that satisfies ``FeedbackStore``: a shared dict standing in for a
    database document. Two of these pointed at the same underlying dict is the multi-process
    case: two ledger instances, one record."""

    def __init__(self, backing: dict):
        self._backing = backing

    def load(self):
        return self._backing.get("payload") or {}

    def save(self, payload):
        self._backing["payload"] = payload


class RaisingStore:
    def load(self):
        raise RuntimeError("store unreachable")

    def save(self, payload):
        raise RuntimeError("store unreachable")


def test_a_store_round_trips_items_like_a_file_would():
    backing = {}
    ledger = FeedbackLedger(store=DictStore(backing))
    ledger.observe(card_id="q1", source="quest_notes", item_id="n1", text="add page numbers",
                    author="joshua", at=NOW)
    ledger.apply_disposition(card_id="q1", source="quest_notes", item_id="n1",
                             disposition="done", at=NOW)

    reopened = FeedbackLedger(store=DictStore(backing))
    item = reopened.get("q1", "quest_notes", "n1")
    assert item is not None
    assert item.text == "add page numbers"
    assert item.state == DONE


def test_two_ledger_instances_sharing_one_store_see_each_others_writes():
    """The multi-process case: a live product surface and a background lane, each holding their
    own FeedbackLedger, backed by the SAME store. A write from one must be visible to the other's
    very next read, exactly as the file store's reload-under-lock makes true for two processes on
    one path."""
    backing = {}
    writer = FeedbackLedger(store=DictStore(backing), requires_acceptance=True)
    reader = FeedbackLedger(store=DictStore(backing))

    writer.observe(card_id="q1", source="quest_notes", item_id="n1", text="add a status column",
                    at=NOW)
    assert reader.get("q1", "quest_notes", "n1") is not None

    writer.apply_disposition(card_id="q1", source="quest_notes", item_id="n1",
                             disposition="done", by="run", at=NOW)
    seen_by_reader = reader.get("q1", "quest_notes", "n1")
    assert seen_by_reader.state == AWAITING_ACCEPTANCE, (
        "a run's 'done' on a store that requires acceptance must be visible to a second "
        "instance as awaiting acceptance, not as done"
    )

    reader.accept(card_id="q1", source="quest_notes", item_id="n1", at=NOW)
    assert writer.get("q1", "quest_notes", "n1").state == ACCEPTED, (
        "a person's acceptance recorded through one instance must be visible through the other"
    )


def test_a_broken_store_degrades_like_a_missing_file_would():
    ledger = FeedbackLedger(store=RaisingStore())
    # Read: empty, not an exception.
    assert ledger.for_card("q1") == []
    # Write: swallowed and logged, not raised into the caller.
    item = ledger.observe(card_id="q1", source="quest_notes", item_id="n1", text="hello", at=NOW)
    assert item is not None
    assert item.text == "hello"


def test_a_store_and_a_path_are_mutually_exclusive_the_store_wins():
    """A consumer that hands over a store is opting all the way out of file storage -- ``path`` is
    only ever a human-readable label at that point, never a second place data could land."""
    backing = {}
    ledger = FeedbackLedger(path="/should/never/be/touched.json", store=DictStore(backing))
    ledger.observe(card_id="q1", source="quest_notes", item_id="n1", text="hi", at=NOW)
    assert "payload" in backing
    import os
    assert not os.path.exists("/should/never/be/touched.json")
