"""Proof that extracting ``StateStore`` out of ``runner/poller.py`` into its own module
(``runner/state_store.py``) was a MECHANICAL extraction, not a behavior change.

``tests/test_statestore_edge_cases.py`` already exercises ``StateStore``'s actual behavior in
depth (rotation, concurrency, atomic writes, corruption, the 5000-signature cap) via
``from quest_ai_runner.runner.poller import StateStore`` -- unchanged, and still green after the
extraction (see the module docstring on ``runner/state_store.py``). This file pins the two things
that test suite doesn't: that both import paths resolve to the EXACT SAME class object (so
``poller.StateStore`` and ``channel_runner``'s use of ``state_store.StateStore`` share the one
implementation, never a fork), and that ``runner.channel_runner.ChannelRunner`` -- the new
consumer this extraction was FOR -- actually builds its dedup store from the shared class.
"""
from __future__ import annotations

import tempfile

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner import state_store as state_store_module
from quest_ai_runner.runner.channel_runner import ChannelRunner
from quest_ai_runner.runner.poller import StateStore as PollerStateStore


def test_poller_reexports_the_same_class_object():
    """`from quest_ai_runner.runner.poller import StateStore` (every existing caller/test) must
    resolve to the literal same class as the new home, not a re-implementation or a copy."""
    assert PollerStateStore is state_store_module.StateStore


def test_channel_runner_uses_the_shared_state_store():
    cfg = RunnerConfig(channel_allowed_senders=["alice"])
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = f"{tmpdir}/channel_state.json"
        runner = ChannelRunner(cfg, state_path=state_path)
        assert isinstance(runner.state, state_store_module.StateStore)
        assert isinstance(runner.state, PollerStateStore)  # same class, either name
        # Prove it is actually the SAME dedup mechanism (mark/seen), not just the same type.
        assert runner.state.seen("openclaw:m1") is False
        runner.state.mark("openclaw:m1")
        assert runner.state.seen("openclaw:m1") is True
        runner.close()
