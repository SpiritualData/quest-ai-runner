"""Offline tests for the persist_cards conformance set: the filesystem, Quest API and embedded Qdrant backends keep
every case (the Qdrant listing once stopped at a silent card cap; the large-collection case pins that it does not),
and a repository that loses or duplicates cards is caught by the cases.
Run: .venv/bin/python3 -m pytest evaluation/components/test_persist_cards.py -q -p no:cacheprovider"""
import importlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from persist_cards.backends import FolderHarness, on_backend  # noqa: E402

CASES = json.loads((HERE / "persist_cards" / "cases.json").read_text())


def failed(fn):
    score = importlib.import_module("persist_cards.score")
    return {c["id"] for c in CASES if not score.score_case(fn, c)["ok"]}


@pytest.mark.parametrize("impl,expected", [("filesystem", set()), ("quest_api", set()), ("qdrant", set())])
def test_backend_keeps_the_contract(impl, expected):
    fn = common.load_impl(impl, "persist_cards")(common.CallMeter(max_calls=0))
    assert failed(fn) == expected


class LossyRepo:
    """Drops every third write and keeps a duplicate of an id rewritten (the failures the cases exist to catch)."""

    def __init__(self):
        self.cards, self.writes = {}, 0

    def load_all(self):
        return dict(self.cards)

    def read(self, card_id):
        return self.cards.get(card_id)

    def write(self, card_id, card):
        self.writes += 1
        if self.writes % 3 == 0:
            return True
        self.cards[card_id] = card
        return True

    def delete(self, card_id):
        self.cards.pop(card_id, None)
        return True

    def exists(self, card_id):
        return card_id in self.cards

    def revision(self):
        return len(self.cards)


def test_a_lossy_repository_is_caught():
    fn = on_backend(FolderHarness, lambda harness: LossyRepo())
    assert {"PCD-001", "PCD-003", "PCD-004"} <= failed(fn)
