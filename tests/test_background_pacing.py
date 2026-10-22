"""Work nobody waits for pauses for real, so the threads that are waiting get through.

Measured with a busy thread beside one making 3,000 stat calls: 8.1 s against 4 ms idle; ``sleep(0)``
every 200 items changed nothing (7.4 s); a 2 ms pause after each 30 ms of work cost the busy thread
about 6% and brought the other to 35 ms. On a cold 25,000-card store the same pause took a thread's
3,000 blocking calls from 9.3 s to 73 ms while the index build went from 28.0 s to 29.4 s.

Fully offline.
"""
from __future__ import annotations

import os
import threading
import time
from typing import List

import pytest

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.core import background_pacing
from quest_ai_runner.core.background_pacing import PAUSE_SECONDS, WORK_SECONDS, Pacer


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


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(background_pacing.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(background_pacing.time, "sleep", fake.sleep)
    return fake


def test_a_pacer_pauses_after_each_stretch_of_work(clock):
    pace = Pacer(work_seconds=1.0, pause_seconds=0.25)
    for _ in range(10):
        clock.now += 0.5
        pace()
    assert clock.pauses == [0.25] * 5


def test_the_default_stretch_and_pause_are_the_measured_ones(clock):
    pace = Pacer()
    clock.now += WORK_SECONDS / 2
    pace()
    assert clock.pauses == []
    clock.now += WORK_SECONDS
    pace()
    assert clock.pauses == [PAUSE_SECONDS]


def test_a_pacer_that_never_paces_never_sleeps(clock):
    pace = Pacer.never()
    for _ in range(10):
        clock.now += WORK_SECONDS * 3
        pace()
    assert clock.pauses == []


def test_a_pacer_stays_out_of_the_way_until_its_start_time(clock):
    pace = Pacer(start_after=clock.now + 5.0, work_seconds=2.0, pause_seconds=0.25)
    for _ in range(6):
        clock.now += 1.0
        pace()
    # Work before the start time does not count towards a pause: the first stretch begins when it passes.
    assert clock.pauses == [0.25]
    clock.now += 2.0
    pace()
    assert clock.pauses == [0.25, 0.25]


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


def blocking_calls(count: int) -> float:
    started = time.perf_counter()
    for _ in range(count):
        os.stat(".")
    return time.perf_counter() - started


def time_blocking_calls_beside_busy_thread(pace: Pacer, count: int = 150) -> float:
    stop = threading.Event()

    def busy() -> None:
        while not stop.is_set():
            sum(i * i for i in range(20000))
            pace()

    thread = threading.Thread(target=busy, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        return blocking_calls(count)
    finally:
        stop.set()
        thread.join()


def test_a_paced_thread_leaves_the_others_their_blocking_calls():
    unpaced = time_blocking_calls_beside_busy_thread(Pacer.never())
    paced = time_blocking_calls_beside_busy_thread(Pacer())
    assert paced < unpaced / 4, f"paced {paced:.3f}s, unpaced {unpaced:.3f}s"
