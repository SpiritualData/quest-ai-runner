"""GuidanceProvider.select() must never hang a turn indefinitely.

``select()`` is a caller-supplied ``GuidanceProviderBase`` implementation (a consumer's
``dynamic_guidance_loader`` can hit a DB/network; a consumer's own LLM filtering pass inside
select() can stall). Regression: the orchestrator called it directly and synchronously with no
timeout of its own, right after emitting the turn's "Searching context…" status and before any
later status update -- so a stuck select() looked identical to context assembly stalling (which
IS timeout-protected), but was not protected itself. This is UI-agnostic: it would hang the
orchestrator's turn loop under ANY UI (Textual, ANSI, or a non-interactive caller), not just the
terminal that happened to report it.

Fixed: the call now runs in a bounded ThreadPoolExecutor, collected with
``guidance_selection_timeout_seconds()`` (env ``QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS``, default
5.0s). A timeout degrades to "no guidance this turn" (matching the existing "any failure leaves
the run exactly as if no guidance were wired" contract for a raising select()), not a hung turn.
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional

from quest_ai_runner.core.adapters import GuidanceCard, GuidanceProviderBase
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval


class _HangingGuidance(GuidanceProviderBase):
    """select() blocks well past the test's configured timeout, simulating a stuck
    dynamic_guidance_loader or a stalled provider call inside a real implementation."""

    def __init__(self, block_seconds: float):
        self._block_seconds = block_seconds
        self.select_started = threading.Event()
        self.select_finished = threading.Event()

    def list(self) -> List[GuidanceCard]:
        return []

    def read(self, card_id: str) -> Optional[GuidanceCard]:
        return None

    def select(self, user_message, *, task_type=None, rep_id=None, team_id=None, org_id=None,
               operation=None, function_name=None, tags=None, limit=5) -> List[GuidanceCard]:
        self.select_started.set()
        time.sleep(self._block_seconds)
        self.select_finished.set()  # only reached if NOT abandoned by the timeout
        return []


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


def test_hanging_guidance_select_does_not_hang_the_turn(monkeypatch):
    import quest_ai_runner.core.orchestrator as orch_mod
    monkeypatch.setenv("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS", "0.1")

    guidance = _HangingGuidance(block_seconds=5.0)
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])

    t0 = time.monotonic()
    res = _orch(provider, StubRetrieval(), guidance=guidance).run("hello")
    elapsed = time.monotonic() - t0

    assert res.kind == "answer"
    assert guidance.select_started.is_set()          # the call was actually made
    assert not guidance.select_finished.is_set()      # abandoned at the timeout, not awaited
    # The turn completed close to the configured timeout, nowhere near the 5s block.
    assert elapsed < 2.0
    # No guidance was applied (timed out before returning anything).
    assert "--- APPLICABLE GUIDANCE ---" not in provider.plan_prompts[0]


def test_guidance_selection_timeout_env_default(monkeypatch):
    monkeypatch.delenv("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS", raising=False)
    from quest_ai_runner.core.orchestrator import guidance_selection_timeout_seconds
    assert guidance_selection_timeout_seconds() == 5.0


def test_guidance_selection_timeout_env_override(monkeypatch):
    monkeypatch.setenv("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS", "2.5")
    from quest_ai_runner.core.orchestrator import guidance_selection_timeout_seconds
    assert guidance_selection_timeout_seconds() == 2.5


def test_guidance_selection_timeout_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS", "not-a-number")
    from quest_ai_runner.core.orchestrator import guidance_selection_timeout_seconds
    assert guidance_selection_timeout_seconds() == 5.0


def test_fast_guidance_select_still_applies_normally(monkeypatch):
    """The timeout wrapper must not change the normal, fast-path behavior."""
    from tests.test_guidance import FakeGuidance, _cards
    monkeypatch.setenv("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS", "5")
    g = FakeGuidance(_cards(), select_ids=["quest_creation"])
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    res = _orch(provider, StubRetrieval(), guidance=g).run("I want to start a new quest")
    assert res.kind == "answer"
    assert "--- APPLICABLE GUIDANCE ---" in provider.plan_prompts[0]
