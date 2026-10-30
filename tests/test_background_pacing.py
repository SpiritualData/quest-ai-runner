"""Where the card store paces its background work: an index build nobody waits for pauses, one someone waits for does not.

The pacer itself (``hubofhubs.recall.background_pacing``) is tested in the hub. Fully offline.
"""
from __future__ import annotations

from typing import List

from quest_ai_runner.adapters.file_context_store import FileContextStore
from hubofhubs.recall import background_pacing
from hubofhubs.recall.background_pacing import Pacer


class FakeClock:
    """A clock the test advances itself, and a sleep that records the pauses instead of taking them."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def test_the_default_does_not_pause_the_index_build_someone_is_waiting_on(tmp_path, monkeypatch):
    sleeps: List[float] = []
    monkeypatch.setattr(background_pacing.time, "sleep", sleeps.append)
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    cards = {f"c{i}": {"id": f"c{i}", "name": f"Card {i}", "keywords": [f"k{i}"]} for i in range(50)}
    store._scoring_index(cards)
    assert sleeps == []


class TickingClock(FakeClock):
    """Every read of the clock is a second later, so a pacer that is allowed to pause always does."""

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now


def build_index(tmp_path, monkeypatch, meta):
    ticking = TickingClock()
    monkeypatch.setattr(background_pacing.time, "monotonic", ticking.monotonic)
    monkeypatch.setattr(background_pacing.time, "sleep", ticking.sleep)
    store = FileContextStore(str(tmp_path), auto_bootstrap=False, confidence_threshold=0.0)
    for i in range(6):
        store._repo.write(f"c{i}", {"id": f"c{i}", "name": f"Topic {i}", "keywords": ["alpha", f"k{i}"]})
    store.assemble("alpha question", meta=meta)
    return ticking.pauses


def test_an_index_build_the_turn_gave_up_on_paces_itself(tmp_path, monkeypatch):
    pauses = build_index(tmp_path, monkeypatch, {"assembly_deadline": 0.0})
    assert len(pauses) >= 5, "once the deadline has passed, each card weighed offers a pause and takes it"


def test_an_index_build_someone_is_waiting_on_is_not_paced(tmp_path, monkeypatch):
    assert build_index(tmp_path, monkeypatch, None) == []


def test_the_prewarm_is_paced(tmp_path, monkeypatch):
    made: List[Pacer] = []
    real = Pacer

    def record(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr("quest_ai_runner.adapters.context_store_bootstrap.Pacer", record)
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    store._repo.write("c0", {"id": "c0", "name": "Topic", "keywords": ["alpha"]})
    store.prewarm()
    assert len(made) == 1 and made[0].start_after == 0.0
