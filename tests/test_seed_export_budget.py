"""The vector seed's budget is applied BEFORE the per-card work, not after it.

A card store of 142,781 cards was exported in full on the seed thread (an embed item built for every
card: text, payload, fingerprint) and then cut to ``max_seed_items`` (1,000) by the vector arm, so
the thread competed with the first turn's context assembly to build 141,781 items nobody used, and
the 1,000 that were kept were whichever came first in dict order. ``export_for_embedding(limit=N)``
now ranks the cards by a cheap key (managed, then used, the most recently used or changed first) and
builds items for only the best N. These cases pin that, and that every call without a limit is
exactly what it was.

Fully offline: a fake vector store, no embedder, no network.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

import quest_ai_runner.adapters.context_store_cards as store_cards
from hubofhubs.recall.card_origin import last_touched as card_last_touched
from quest_ai_runner.adapters.context_store_cards import (
    card_seed_key,
    cards_in_seed_order,
)
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.vector_context_assembler import (
    VectorContextAssembler,
    seed_source_takes_limit,
)
from hubofhubs.ports.context import VectorStoreBase
from quest_ai_runner.core.vector_scopes import SeedItems


def make_card(card_id: str, *, used: int = 0, touched: float = 0.0, managed: bool = False,
              **fields: Any) -> Dict[str, Any]:
    """A bootstrap-shaped card. ``touched`` is the pinned file's mtime (when it last changed)."""
    card: Dict[str, Any] = {
        "id": card_id,
        "name": f"Topic {card_id}",
        "keywords": [f"kw{card_id}"],
        "summary": f"About {card_id}",
        "files": [{"path": f"pkg/{card_id}.py", "sha256": f"sha-{card_id}", "mtime": touched,
                   "symbols": [f"sym_{card_id}"]}],
        "usage_count": used,
        "provenance": {"created_by_task": "bootstrap", "last_verified_at": ""},
    }
    if managed:
        card["managed_by"] = "quest-folder-sync"
    card.update(fields)
    return card


def make_store(tmp_path, cards: List[Dict[str, Any]]) -> FileContextStore:
    store = FileContextStore(str(tmp_path / "cards"), auto_bootstrap=False)
    for card in cards:
        assert store.write_card(card["id"], card)
    return store


def exported_ids(items: List[Dict[str, Any]]) -> List[str]:
    return [item["id"][len("card:"):] for item in items]


class RecordingVectorStore(VectorStoreBase):
    """Records what a seed pass hands to ``sync``."""

    def __init__(self) -> None:
        self.synced: List[Dict[str, Any]] = []

    def search(self, query, *, scope=None, top_k=8):
        return []

    def upsert(self, items, *, scope=None):
        pass

    def sync(self, items, *, scope=None):
        self.synced.extend(items)
        return len(items)


@pytest.fixture
def embed_text_calls(monkeypatch):
    """Counts the per-card embed-text builds, the first and costliest step of building an item."""
    calls: List[str] = []
    real = store_cards.card_embed_text

    def counting(card):
        calls.append(card.get("id", ""))
        return real(card)

    monkeypatch.setattr(store_cards, "card_embed_text", counting)
    return calls


class TestLimitedExportBuildsOnlyWhatItKeeps:
    def test_items_are_built_for_only_that_many_cards(self, tmp_path, embed_text_calls):
        store = make_store(tmp_path, [make_card(f"c{i:02d}", touched=float(i)) for i in range(12)])
        items = store.export_for_embedding(limit=5)
        assert len(items) == 5
        assert len(embed_text_calls) == 5, "an item was built for a card that was then dropped"

    def test_the_most_recently_used_cards_are_the_ones_kept(self, tmp_path):
        cards = [
            make_card("old", touched=100.0),
            make_card("changed-lately", touched=900.0),
            make_card("learned-lately", content=[{"id": "n1", "type": "note", "ts": 800.0}]),
            make_card("used-lately", content=[{"id": "n2", "type": "note", "last_used_ts": 1000.0}]),
            make_card("verified-lately", provenance={"last_verified_at": "1970-01-01T00:20:00+00:00"}),
            make_card("older", touched=200.0),
        ]
        store = make_store(tmp_path, cards)
        kept = exported_ids(store.export_for_embedding(limit=4))
        assert sorted(kept) == ["changed-lately", "learned-lately", "used-lately", "verified-lately"]

    def test_managed_cards_then_used_cards_come_before_newer_unused_ones(self, tmp_path):
        cards = [
            make_card("fresh-a", touched=5000.0),
            make_card("fresh-b", touched=6000.0),
            make_card("used-old", used=3, touched=10.0),
            make_card("quest-card", managed=True),   # a managed card carries no timestamp at all
        ]
        store = make_store(tmp_path, cards)
        assert exported_ids(store.export_for_embedding(limit=2)) == ["quest-card", "used-old"]
        assert exported_ids(store.export_for_embedding(limit=3))[2] == "fresh-b"

    def test_with_nothing_to_tell_cards_apart_the_choice_is_stable(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in (7, 3, 9, 1, 5)])
        assert exported_ids(store.export_for_embedding(limit=3)) == ["c01", "c03", "c05"]

    def test_the_result_says_how_many_cards_the_store_holds(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(12)])
        items = store.export_for_embedding(limit=5)
        assert isinstance(items, SeedItems)
        assert items.offered == 12

    def test_a_kept_card_gets_the_same_item_as_without_a_limit(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}", touched=float(i)) for i in range(8)])
        everything = {item["id"]: item for item in store.export_for_embedding()}
        for item in store.export_for_embedding(limit=3):
            assert item == everything[item["id"]]


class TestEveryCallWithoutALimitIsUnchanged:
    def test_no_argument_exports_every_card_in_store_order_as_a_plain_list(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}", touched=float(i)) for i in range(9)])
        items = store.export_for_embedding()
        assert type(items) is list
        assert exported_ids(items) == list(store._own_cards())

    def test_none_and_zero_mean_no_limit(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(6)])
        everything = store.export_for_embedding()
        assert store.export_for_embedding(limit=None) == everything
        assert store.export_for_embedding(limit=0) == everything

    def test_a_store_within_the_limit_is_exported_whole_and_in_store_order(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}", touched=float(i)) for i in range(6)])
        everything = store.export_for_embedding()
        within = store.export_for_embedding(limit=6)
        assert within == everything
        assert exported_ids(within) == exported_ids(everything), "a store under the cap was reordered"
        assert within.offered == 6

    def test_a_store_within_the_limit_pays_for_no_ranking(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(4)])

        def no_ranking(cards):
            raise AssertionError("a store within the limit was ranked")

        monkeypatch.setattr(store_cards, "cards_in_seed_order", no_ranking)
        assert len(store.export_for_embedding(limit=10)) == 4

    def test_a_card_with_nothing_to_embed_is_still_skipped(self, tmp_path):
        blank = {"id": "blank", "files": [], "usage_count": 0}
        store = make_store(tmp_path, [make_card("real"), blank])
        assert exported_ids(store.export_for_embedding()) == ["real"]


class TestTheVectorArmHandsTheCeilingToTheSource:
    def seed(self, store, cap, caplog=None):
        vector = RecordingVectorStore()
        arm = VectorContextAssembler(vector, seed_source=store.export_for_embedding,
                                     max_seed_items=cap, confidence_min_score=0.0)
        if caplog is not None:
            with caplog.at_level(logging.WARNING):
                arm.assemble("anything")
        else:
            arm.assemble("anything")
        return vector

    def test_only_the_ceiling_is_built_and_the_warning_names_the_whole_store(
            self, tmp_path, embed_text_calls, caplog):
        store = make_store(tmp_path, [make_card(f"c{i:02d}", touched=float(i)) for i in range(10)])
        vector = self.seed(store, 3, caplog)
        assert len(vector.synced) == 3
        assert len(embed_text_calls) == 3
        assert exported_ids(vector.synced) == ["c09", "c08", "c07"]
        assert "Card store has 10 cards; only the first 3 are searched by meaning" in caplog.text
        assert "quarantine_cards.py" in caplog.text

    def test_a_store_within_the_ceiling_seeds_whole_and_is_quiet(self, tmp_path, caplog):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(4)])
        vector = self.seed(store, 10, caplog)
        assert len(vector.synced) == 4
        assert "Card store has" not in caplog.text

    def test_no_ceiling_asks_for_everything(self, tmp_path):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(7)])
        assert len(self.seed(store, 0).synced) == 7

    def test_a_source_that_takes_a_limit_is_given_the_ceiling(self):
        asked: List[Optional[int]] = []

        def source(limit=None):
            asked.append(limit)
            return []

        VectorContextAssembler(RecordingVectorStore(), seed_source=source,
                               max_seed_items=25).assemble("anything")
        VectorContextAssembler(RecordingVectorStore(), seed_source=source,
                               max_seed_items=0).assemble("anything")
        assert asked == [25, None]

    def test_a_zero_argument_source_is_still_called_with_no_arguments(self):
        asked: List[str] = []

        def source():
            asked.append("called")
            return [{"id": "card:x", "text": "x", "payload": {}, "fingerprint": "f"}]

        vector = RecordingVectorStore()
        VectorContextAssembler(vector, seed_source=source, max_seed_items=1,
                               confidence_min_score=0.0).assemble("anything")
        assert asked == ["called"] and len(vector.synced) == 1

    def test_which_sources_take_a_limit(self, tmp_path):
        assert seed_source_takes_limit(make_store(tmp_path, []).export_for_embedding)
        assert seed_source_takes_limit(lambda limit=None: [])
        assert not seed_source_takes_limit(lambda: [])
        assert not seed_source_takes_limit(lambda *args, **kwargs: [])
        assert not seed_source_takes_limit(object())


class CountingPacer:
    """Stands in for ``Pacer``: counts how often the loop under test offers to pause."""

    made: List["CountingPacer"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls = 0
        CountingPacer.made.append(self)

    def __call__(self) -> None:
        self.calls += 1


class TestTheExportLetsTheTurnRun:
    def test_the_export_offers_to_pause_once_per_card(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, [make_card(f"c{i:02d}") for i in range(7)])
        CountingPacer.made = []
        monkeypatch.setattr(store_cards, "Pacer", CountingPacer)
        store.export_for_embedding()
        assert [p.calls for p in CountingPacer.made] == [7]

    def test_ranking_offers_to_pause_per_card_and_still_orders_exactly(self, monkeypatch):
        cards = {f"c{i:02d}": make_card(f"c{i:02d}", used=i % 3, touched=float((i * 7) % 11),
                                        managed=(i == 4))
                 for i in range(25)}
        CountingPacer.made = []
        monkeypatch.setattr(store_cards, "Pacer", CountingPacer)
        monkeypatch.setattr(store_cards, "RANK_SLICE", 4)
        ordered = [card_id for card_id, _ in cards_in_seed_order(cards)]
        assert [p.calls for p in CountingPacer.made] == [25]
        assert ordered == [key[3] for key in sorted(card_seed_key(i, c) for i, c in cards.items())]
        assert ordered[0] == "c04"                               # the managed card

    def test_ranking_is_lazy_for_the_caller_that_stops_early(self):
        cards = {f"c{i:02d}": make_card(f"c{i:02d}", touched=float(i)) for i in range(20)}
        first = next(iter(cards_in_seed_order(cards)))
        assert first[0] == "c19"


class TestCardLastTouched:
    def test_a_card_with_no_stamps_has_none(self):
        assert card_last_touched({}) == 0.0
        assert card_last_touched({"files": [{"path": "a.py"}], "content": []}) == 0.0

    def test_the_newest_stamp_anywhere_on_the_card_wins(self):
        card = {
            "content": [{"ts": 10.0, "last_used_ts": 40.0}, {"ts": 30.0}],
            "files": [{"mtime": 20.0, "last_used_ts": 35.0}],
        }
        assert card_last_touched(card) == 40.0

    @pytest.mark.parametrize("verified,expected", [
        ("1200.5", 1200.5),
        ("1970-01-01T00:10:00+00:00", 600.0),
        ("", 0.0),
        ("yesterday", 0.0),
    ])
    def test_last_verified_at_may_be_a_number_an_iso_time_or_junk(self, verified, expected):
        assert card_last_touched({"provenance": {"last_verified_at": verified}}) == expected

    def test_malformed_cards_never_raise(self):
        assert card_last_touched({"files": ["not-a-dict", None], "content": "text"}) == 0.0
        assert card_last_touched({"files": [{"mtime": "later"}], "provenance": "x"}) == 0.0
        assert card_seed_key("c", {"usage_count": "many"})[0] == 2
