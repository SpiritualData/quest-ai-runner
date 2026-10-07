"""Offline unit tests for the per-case CARD SWEEP's selection and settling.

The brain's card updater runs in a background thread and finishes after the HTTP response a case
already read, so a card write can land AFTER that case's sweep and be inherited by the next case
(seen 2026-10-07: a card learned in one case rewrote answers about another quest). The sweep now
waits for the card set to go quiet and runs twice, once after judging and once right before the next
case starts. These tests pin WHICH cards it selects and WHEN it stops waiting; no network, no real
sleeping.

Run:  .venv/bin/python3 -m pytest evaluation/qualitative/test_card_sweep.py -q

Kept beside the harness (not under tests/) for the same reason as the other two files here: importing
``world`` pulls in ``devclient``, which only loads against this machine's dev-lane env, so these are
not part of the library's default offline suite (``testpaths = ["tests"]``).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import world as W  # noqa: E402


# --------------------------------------------------------------------------- selection


def test_selects_only_cards_absent_from_the_baseline():
    baseline = ["card-a", "card-b"]
    assert W.cards_created_since(baseline, {"card-a", "card-b", "card-new"}) == ["card-new"]
    assert W.cards_created_since(baseline, {"card-a", "card-b"}) == []


def test_selects_nothing_without_a_baseline():
    # With no baseline the sweep cannot tell a card the run created from one that pre-dates it, so
    # it must delete NOTHING rather than sweep broadly (documented in the README's known limits).
    assert W.cards_created_since(None, {"card-a", "anything"}) == []


def test_a_card_the_baseline_no_longer_has_is_not_resurrected():
    # A pre-existing card deleted by hand mid-run is simply absent; nothing is selected for it.
    assert W.cards_created_since(["card-a", "card-b"], {"card-a"}) == []


def test_selection_is_sorted_and_stable():
    got = W.cards_created_since([], {"c", "a", "b"})
    assert got == ["a", "b", "c"]


# --------------------------------------------------------------------------- settling


class FakeClock:
    """A monotonic clock advanced only by the fake sleep, so the test never really waits."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_waits_for_a_late_card_write_then_returns_the_settled_set():
    clock = FakeClock()
    # The card the background updater writes appears only on the third listing.
    sets = [{"a"}, {"a"}, {"a", "late"}, {"a", "late"}, {"a", "late"}, {"a", "late"},
            {"a", "late"}, {"a", "late"}, {"a", "late"}, {"a", "late"}]
    calls = {"n": 0}

    def fetch():
        i = min(calls["n"], len(sets) - 1)
        calls["n"] += 1
        return sets[i]

    settled = W.settle_card_set(fetch, quiet_seconds=1.0, timeout=10.0,
                                sleep=clock.sleep, clock=clock.now)
    assert settled == {"a", "late"}
    assert calls["n"] > 2, "it stopped polling before the late write appeared"


def test_returns_at_once_when_the_set_never_changes():
    clock = FakeClock()
    settled = W.settle_card_set(lambda: {"a"}, quiet_seconds=1.0, timeout=10.0,
                                sleep=clock.sleep, clock=clock.now)
    assert settled == {"a"}
    assert clock.t <= 2.0, "it waited longer than the quiet window for a set that never moved"


def test_gives_up_at_the_timeout_rather_than_waiting_forever():
    clock = FakeClock()
    churn = {"n": 0}

    def never_quiet():
        churn["n"] += 1
        return {f"card-{churn['n']}"}

    settled = W.settle_card_set(never_quiet, quiet_seconds=1.0, timeout=3.0,
                                sleep=clock.sleep, clock=clock.now)
    assert isinstance(settled, set)
    assert clock.t <= 4.0, "the timeout did not bound the wait"


def test_a_settled_set_feeds_the_selection():
    # The two halves together: wait for quiet, then select against the baseline.
    clock = FakeClock()
    settled = W.settle_card_set(lambda: {"card-a", "card-new"}, quiet_seconds=1.0, timeout=5.0,
                                sleep=clock.sleep, clock=clock.now)
    assert W.cards_created_since(["card-a"], settled) == ["card-new"]
