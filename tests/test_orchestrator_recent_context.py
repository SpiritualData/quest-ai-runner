"""Offline tests for the Orchestrator's wiring of the WARM recent-context fallback
(see core/recent_context.py): loading + gating recent cards each turn, merging survivors into
context_view/EVENT_CONTEXT ahead of or alongside fresh assembly, and writing the merged selection
back to the store. Uses the same StubProvider/StubRetrieval doubles as test_orchestrator.py and a
real FileRecentContextStore rooted at tmp_path (no real paths/ids)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import EVENT_CONTEXT, Orchestrator, OrchestratorConfig
from quest_ai_runner.core.recent_context import FileRecentContextStore

from .conftest import StubProvider, StubRetrieval


class _CapturingSink:
    """A minimal ProgressSink that records every event the orchestrator emits."""

    def __init__(self):
        self.events: List[Any] = []

    def update(self, event, mode):
        self.events.append(event)


class _FixedCardAssembler:
    """A ContextAssembler stub that returns a fixed set of card_metadata."""

    def __init__(self, card_metadata: List[Dict[str, Any]]):
        self._card_metadata = card_metadata

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext(context_view="ASSEMBLED CONTEXT", card_metadata=self._card_metadata)

    def record(self, task_text: str, outcome: dict) -> None:
        pass


class _RaisingAssembler:
    """A ContextAssembler stub whose assemble() always raises.

    The Orchestrator runs assemble() in a background thread and collects it with
    ``future.result(timeout=5.0)``; an exception raised inside the thread is re-raised at
    collection time and caught by the same except-branch a real timeout would hit, so this
    exercises the identical "no fresh context available" code path as a timeout without an
    actual multi-second sleep in the test suite.
    """

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        raise RuntimeError("boom - simulated assembler failure")

    def record(self, task_text: str, outcome: dict) -> None:
        pass


def _one_answer_provider() -> StubProvider:
    """A provider that answers the request directly (no reads/deep/confirm/verification)."""
    return StubProvider(decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}])


def _pricing_card(adapter: str = "keyword", preview: str = "We offer three pricing tiers.") -> Dict[str, Any]:
    return {
        "id": "pricing-card",
        "title": "Pricing tiers",
        "adapter": adapter,
        "relevance_score": 0.8,
        "files": [],
        "rendered_section": preview,
    }


def _context_events(sink: _CapturingSink):
    return [e for e in sink.events if e.type == EVENT_CONTEXT]


# ---------------------------------------------------------------------------
# (a) Turn 1 records the cards selected by fresh assembly.
# ---------------------------------------------------------------------------


def test_turn_records_selected_cards_to_recent_store(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([_pricing_card()]), recent_context=store,
    )

    res = orch.run("what are our pricing tiers", quest_id="conv-1")

    assert res.kind == "answer"
    loaded = store.load("conv-1")
    ids = {r["id"] for r in loaded}
    assert ids == {"pricing-card"}


def test_turn_does_not_record_when_no_cards_survived(tmp_path):
    """No assembler, no recent store history -> nothing to merge -> record() is never called."""
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )

    orch.run("some brand new question with no history", quest_id="conv-empty")

    assert store.load("conv-empty") == []


# ---------------------------------------------------------------------------
# (b) A follow-up turn 2 gets recent cards in EVENT_CONTEXT, even when the fresh
#     assembler is None or raises (same code path a timeout takes).
# ---------------------------------------------------------------------------


def test_followup_gets_recent_cards_when_assembler_is_none(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-2", [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("what about that", quest_id="conv-2", sink=sink)  # short follow-up, <= 5 words

    assert res.kind == "answer"
    events = _context_events(sink)
    assert events, "EVENT_CONTEXT should fire from surviving recent cards even with no assembler wired"
    card_meta = events[0].data["card_metadata"]
    assert any(c["id"] == "pricing-card" and c["adapter"] == "recent" for c in card_meta)


def test_followup_gets_recent_cards_when_assembler_raises(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-3", [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_RaisingAssembler(), recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("what about that", quest_id="conv-3", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    assert events, "EVENT_CONTEXT should fire from surviving recent cards even when assembly raises"
    card_meta = events[0].data["card_metadata"]
    assert any(c["id"] == "pricing-card" and c["adapter"] == "recent" for c in card_meta)


# ---------------------------------------------------------------------------
# (c) An unrelated (non-follow-up) turn 2 gets no recent cards.
# ---------------------------------------------------------------------------


def test_unrelated_followup_turn_gets_no_recent_cards(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-4", [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("Please explain your data privacy retention documentation", quest_id="conv-4", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    # No fresh assembly ran and no recent card survived the relevance gate -> no EVENT_CONTEXT at all.
    assert not events


# ---------------------------------------------------------------------------
# (d) A card id present in fresh assembly is not duplicated by the recent-turn merge.
# ---------------------------------------------------------------------------


def test_fresh_assembly_card_not_duplicated_by_recent_merge(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-5", [_pricing_card(preview="old preview")], "what are our pricing tiers")

    fresh_card = _pricing_card(adapter="vector", preview="fresh preview")
    fresh_card["title"] = "Pricing tiers (fresh)"
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("what about that", quest_id="conv-5", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    assert events
    card_meta = events[0].data["card_metadata"]
    matching = [c for c in card_meta if c["id"] == "pricing-card"]
    assert len(matching) == 1  # not duplicated: the recent-turn survivor was dropped as a re-find
    assert matching[0]["adapter"] == "vector"  # the FRESH card wins, not the stale "recent" one


# ---------------------------------------------------------------------------
# (e) recent_context_enabled=False disables both load and record.
# ---------------------------------------------------------------------------


def test_recent_context_disabled_flag_skips_load(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-6", [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
        config=OrchestratorConfig(recent_context_enabled=False),
    )
    sink = _CapturingSink()

    res = orch.run("what about that", quest_id="conv-6", sink=sink)

    assert res.kind == "answer"
    assert not _context_events(sink), "disabled flag must skip loading recent cards entirely"


def test_recent_context_disabled_flag_skips_record(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    # Seed the store as if a PRIOR turn (with the flag enabled) had already recorded a card.
    store.record("conv-7", [_pricing_card()], "what are our pricing tiers")

    fresh_card = _pricing_card()
    fresh_card["id"] = "new-card"
    fresh_card["title"] = "New card"
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
        config=OrchestratorConfig(recent_context_enabled=False),
    )

    orch.run("what about that", quest_id="conv-7")

    loaded = store.load("conv-7")
    ids = {r["id"] for r in loaded}
    assert "new-card" not in ids  # record() never ran while disabled
    assert ids == {"pricing-card"}  # unchanged from the original seed
