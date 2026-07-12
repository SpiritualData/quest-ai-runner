"""Offline tests for the Orchestrator's wiring of the WARM recent-context fallback
(see core/recent_context.py): scoped loading + gating recent cards each turn (and each deep goal),
merging survivors into context_view/EVENT_CONTEXT ahead of or alongside fresh assembly, threading
the item-usage hint into fresh assembly's meta, writing the merged selection back to the store
under every applicable scope, and doing all of that identically under Mode.BACKGROUND. Uses the
same StubProvider/StubRetrieval/StubDeepRunner doubles as test_orchestrator.py and a real
FileRecentContextStore rooted at tmp_path (no real paths/ids).

NOTE ON KEYS: the Orchestrator builds scope keys itself (see ``_recent_scope_keys``) as
``"quest:<quest_id>"`` / ``"conv:<conv_id>"`` / ``"global"``. Tests that seed the store directly
(bypassing the orchestrator) use ``quest_scope_key(...)``/``conv_scope_key(...)`` so the seeded key
matches exactly what the orchestrator will look up.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import EVENT_CONTEXT, Mode, Orchestrator, OrchestratorConfig
from quest_ai_runner.core.recent_context import (
    FileRecentContextStore,
    conv_scope_key,
    quest_scope_key,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


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


class _CapturingMetaAssembler:
    """A ContextAssembler stub that records the ``meta`` it was called with (and returns nothing)."""

    def __init__(self):
        self.last_meta: Optional[Dict[str, Any]] = None
        self.calls = 0

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        self.calls += 1
        self.last_meta = meta
        return AssembledContext()

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
# (a) Turn 1 records the cards selected by fresh assembly, under the quest scope.
# ---------------------------------------------------------------------------


def test_turn_records_selected_cards_to_recent_store(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([_pricing_card()]), recent_context=store,
    )

    res = orch.run("what are our pricing tiers", quest_id="quest-1")

    assert res.kind == "answer"
    loaded = store.load(quest_scope_key("quest-1"))
    ids = {r["id"] for r in loaded}
    assert ids == {"pricing-card"}
    # Also recorded under "global" (on by default).
    assert {r["id"] for r in store.load("global")} == {"pricing-card"}


def test_turn_does_not_record_when_no_cards_survived(tmp_path):
    """No assembler, no recent store history -> nothing to merge -> record() is never called."""
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )

    orch.run("some brand new question with no history", quest_id="quest-empty")

    assert store.load(quest_scope_key("quest-empty")) == []
    assert store.load("global") == []


# ---------------------------------------------------------------------------
# (b) A follow-up turn 2 gets recent cards in EVENT_CONTEXT, even when the fresh
#     assembler is None or raises (same code path a timeout takes).
# ---------------------------------------------------------------------------


def test_followup_gets_recent_cards_when_assembler_is_none(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-2"), [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )
    sink = _CapturingSink()

    # A quest-scoped record has NO free pass (only conv-scope does), so the follow-up must have
    # real lexical overlap with the stored card to survive -- "pricing" does.
    res = orch.run("tell me more about pricing", quest_id="quest-2", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    assert events, "EVENT_CONTEXT should fire from surviving recent cards even with no assembler wired"
    card_meta = events[0].data["card_metadata"]
    assert any(c["id"] == "pricing-card" and c["adapter"] == "recent" for c in card_meta)


def test_followup_gets_recent_cards_when_assembler_raises(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-3"), [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_RaisingAssembler(), recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("tell me more about pricing", quest_id="quest-3", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    assert events, "EVENT_CONTEXT should fire from surviving recent cards even when assembly raises"
    card_meta = events[0].data["card_metadata"]
    assert any(c["id"] == "pricing-card" and c["adapter"] == "recent" for c in card_meta)


# ---------------------------------------------------------------------------
# (c) An unrelated (non-follow-up, no-overlap) turn 2 gets no recent cards.
# ---------------------------------------------------------------------------


def test_unrelated_followup_turn_gets_no_recent_cards(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-4"), [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("Please explain your data privacy retention documentation", quest_id="quest-4", sink=sink)

    assert res.kind == "answer"
    events = _context_events(sink)
    # No fresh assembly ran and no recent card survived the relevance gate -> no EVENT_CONTEXT at all.
    assert not events


# ---------------------------------------------------------------------------
# (d) A card id present in fresh assembly is not duplicated by the recent-turn merge.
# ---------------------------------------------------------------------------


def test_fresh_assembly_card_not_duplicated_by_recent_merge(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-5"), [_pricing_card(preview="old preview")], "what are our pricing tiers")

    fresh_card = _pricing_card(adapter="vector", preview="fresh preview")
    fresh_card["title"] = "Pricing tiers (fresh)"
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
    )
    sink = _CapturingSink()

    res = orch.run("tell me more about pricing", quest_id="quest-5", sink=sink)

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
    store.record(quest_scope_key("quest-6"), [_pricing_card()], "what are our pricing tiers")

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=None, recent_context=store,
        config=OrchestratorConfig(recent_context_enabled=False),
    )
    sink = _CapturingSink()

    res = orch.run("tell me more about pricing", quest_id="quest-6", sink=sink)

    assert res.kind == "answer"
    assert not _context_events(sink), "disabled flag must skip loading recent cards entirely"


def test_recent_context_disabled_flag_skips_record(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    # Seed the store as if a PRIOR turn (with the flag enabled) had already recorded a card.
    store.record(quest_scope_key("quest-7"), [_pricing_card()], "what are our pricing tiers")

    fresh_card = _pricing_card()
    fresh_card["id"] = "new-card"
    fresh_card["title"] = "New card"
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
        config=OrchestratorConfig(recent_context_enabled=False),
    )

    orch.run("tell me more about pricing", quest_id="quest-7")

    loaded = store.load(quest_scope_key("quest-7"))
    ids = {r["id"] for r in loaded}
    assert "new-card" not in ids  # record() never ran while disabled
    assert ids == {"pricing-card"}  # unchanged from the original seed


# ---------------------------------------------------------------------------
# (f) Scope keys: conv + quest + global all consulted; the disabled-global knob.
# ---------------------------------------------------------------------------


def test_recent_scope_keys_include_conv_quest_and_global_by_default():
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=_one_answer_provider(),
        registry=ModelRegistry(_one_answer_provider()),
    )
    keys = orch._recent_scope_keys({"conv_id": "c1", "quest_id": "q1"})
    assert keys == [conv_scope_key("c1"), quest_scope_key("q1"), "global"]


def test_recent_scope_keys_global_only_when_no_conv_or_quest_id():
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=_one_answer_provider(),
        registry=ModelRegistry(_one_answer_provider()),
    )
    assert orch._recent_scope_keys({}) == ["global"]
    assert orch._recent_scope_keys(None) == ["global"]


def test_recent_scope_keys_global_disabled_knob_excludes_global():
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        config=OrchestratorConfig(recent_context_global_enabled=False),
    )
    assert orch._recent_scope_keys({"quest_id": "q1"}) == [quest_scope_key("q1")]
    assert orch._recent_scope_keys({}) == []  # nothing to key on at all with global off


def test_disabled_global_knob_env_wiring(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.delenv("QAR_RECENT_CONTEXT_GLOBAL", raising=False)
    cfg = _config_from_env()
    assert cfg.orchestrator.recent_context_global_enabled is True  # library default: on

    monkeypatch.setenv("QAR_RECENT_CONTEXT_GLOBAL", "false")
    cfg = _config_from_env()
    assert cfg.orchestrator.recent_context_global_enabled is False

    monkeypatch.setenv("QAR_RECENT_CONTEXT_GLOBAL", "true")
    cfg = _config_from_env()
    assert cfg.orchestrator.recent_context_global_enabled is True


# ---------------------------------------------------------------------------
# (g) BACKGROUND mode: the SAME warm load/merge/record path runs identically, and a second
#     background task on the SAME quest (no conv_id at all) warm-starts from the first.
# ---------------------------------------------------------------------------


def test_background_mode_quest_scope_warm_starts_a_later_task(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))

    provider1 = _one_answer_provider()
    orch1 = Orchestrator(
        retrieval=StubRetrieval(), provider=provider1, registry=ModelRegistry(provider1),
        context_assembler=_FixedCardAssembler([_pricing_card()]), recent_context=store,
    )
    res1 = orch1.run("what are our pricing tiers", quest_id="quest-bg", mode=Mode.BACKGROUND)
    assert res1.kind == "answer"

    # A second, INDEPENDENT background task on the SAME quest, no conv_id, no assembler wired at
    # all -- it must still warm-start from the quest-scoped record the first task left behind.
    provider2 = _one_answer_provider()
    orch2 = Orchestrator(
        retrieval=StubRetrieval(), provider=provider2, registry=ModelRegistry(provider2),
        context_assembler=None, recent_context=store,
    )
    sink = _CapturingSink()
    res2 = orch2.run("summarize the pricing tiers for the customer",
                     quest_id="quest-bg", mode=Mode.BACKGROUND, sink=sink)

    assert res2.kind == "answer"
    events = _context_events(sink)
    assert events, "a second BACKGROUND task on the same quest must warm-start from the first"
    card_meta = events[0].data["card_metadata"]
    assert any(c["id"] == "pricing-card" and c["adapter"] == "recent" for c in card_meta)


# ---------------------------------------------------------------------------
# (h) _assemble_for_goal_with_cards: per-goal warm merge + item-usage hint threading.
# ---------------------------------------------------------------------------


def test_assemble_for_goal_merges_fresh_and_recent_survivors(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-goal-1"), [_pricing_card(preview="old preview")],
                "pricing tiers question")

    fresh_card = {"id": "other-card", "title": "Other card", "adapter": "vector", "files": [],
                 "rendered_section": "fresh other content"}
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
    )

    text, cards = orch._assemble_for_goal_with_cards(
        "summarize pricing tiers", ctx_meta={"quest_id": "quest-goal-1"})

    ids = {c["id"] for c in cards}
    assert ids == {"other-card", "pricing-card"}
    assert "fresh other content" in text
    assert "CONTEXT FROM RECENT TURNS" in text


def test_assemble_for_goal_dedupes_fresh_wins(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(quest_scope_key("quest-goal-2"), [_pricing_card(preview="stale preview")],
                "pricing tiers question")

    fresh_card = _pricing_card(adapter="vector", preview="fresh preview")
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
    )

    _text, cards = orch._assemble_for_goal_with_cards(
        "pricing tiers", ctx_meta={"quest_id": "quest-goal-2"})
    matching = [c for c in cards if c["id"] == "pricing-card"]
    assert len(matching) == 1
    assert matching[0]["adapter"] == "vector"  # the fresh card wins over the stale recent one


def test_assemble_for_goal_threads_recent_item_usage_hint(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    items = [{"id": "i1", "type": "note", "locator": {}, "text": "about pricing tiers"}]
    store.record(quest_scope_key("quest-goal-3"),
                [{"id": "pricing-card", "title": "Pricing tiers", "items": items}],
                "pricing tiers question")

    assembler = _CapturingMetaAssembler()
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=assembler, recent_context=store,
    )

    orch._assemble_for_goal_with_cards("pricing tiers goal", ctx_meta={"quest_id": "quest-goal-3"})

    assert assembler.calls == 1
    assert assembler.last_meta is not None
    assert assembler.last_meta.get("recent_item_usage") == {"pricing-card": ["i1"]}


def test_assemble_for_goal_no_hint_key_when_nothing_recent(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    assembler = _CapturingMetaAssembler()
    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=assembler, recent_context=store,
    )

    orch._assemble_for_goal_with_cards("brand new goal", ctx_meta={"quest_id": "quest-goal-4"})

    assert assembler.last_meta is not None
    assert "recent_item_usage" not in assembler.last_meta


def test_assemble_for_goal_returns_empty_when_nothing_wired():
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=_one_answer_provider(),
        registry=ModelRegistry(_one_answer_provider()),
    )
    text, cards = orch._assemble_for_goal_with_cards("a goal", ctx_meta={})
    assert text == ""
    assert cards == []


# ---------------------------------------------------------------------------
# (i) A completed deep goal records its context back to the store (under the quest scope), so a
#     LATER goal on the same quest warm-starts.
# ---------------------------------------------------------------------------


def test_deep_goal_records_its_context_for_a_later_goal(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Summarize pricing tiers", "deep_brief": "do it",
         "rationale": "real work"},
        {"met": True, "reason": "done"},  # goal verification
    ])
    runner = StubDeepRunner(met=True, output="done")
    fresh_card = _pricing_card()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
        deep_runner=runner,
    )

    res = orch.run("Summarize pricing tiers", quest_id="quest-deep-1")

    assert res.kind == "deep"
    assert res.deep_results and res.deep_results[0].met is True
    loaded = store.load(quest_scope_key("quest-deep-1"))
    assert any(r["id"] == "pricing-card" for r in loaded)


def test_deep_goal_does_not_record_when_recent_context_disabled(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Summarize pricing tiers", "deep_brief": "do it",
         "rationale": "real work"},
    ])
    runner = StubDeepRunner(met=True, output="done")
    fresh_card = _pricing_card()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        context_assembler=_FixedCardAssembler([fresh_card]), recent_context=store,
        deep_runner=runner, config=OrchestratorConfig(recent_context_enabled=False),
    )

    orch.run("Summarize pricing tiers", quest_id="quest-deep-2")

    assert store.load(quest_scope_key("quest-deep-2")) == []
