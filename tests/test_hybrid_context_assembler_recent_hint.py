"""Offline tests for HybridContextAssembler's threading of the ``recent_item_usage`` hint (see
core/recent_context.py's ``build_item_usage_hint``) into the consolidating LLM pass, and for the
item-ORDER the consolidator returns actually reaching the rebuilt ``rendered_section`` (not just
the returned ``items`` list) -- see ``_consolidate_merged``'s ``_reordered_section`` helper.

No network: a scripted stub ``ModelProvider`` plays the consolidator's LLM call and records the
last prompt it was given.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.card_content_render import render_block_lines
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.core.adapters import AssembledContext


class _FixedAssembler:
    """A ContextAssembler stub returning a fixed set of card_metadata as the "keyword" arm."""

    def __init__(self, card_metadata: List[Dict[str, Any]]):
        self._card_metadata = card_metadata

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext(
            context_view="stub keyword context",
            card_ids=[c["id"] for c in self._card_metadata],
            card_metadata=self._card_metadata,
        )

    def record(self, task_text: str, outcome: dict) -> None:
        pass


class _EmptyAssembler:
    """A ContextAssembler stub standing in for the vector arm (finds nothing)."""

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext()

    def record(self, task_text: str, outcome: dict) -> None:
        pass


class _ScriptedProvider:
    """A ModelProvider whose ``answer()`` returns a fixed scripted string and records the prompt."""

    def __init__(self, scripted: str):
        self._scripted = scripted
        self.last_prompt = ""
        self.answer_calls = 0

    def plan(self, prompt, *, model, tool_schema):  # pragma: no cover - unused here
        return {"action": "answer"}

    def answer(self, messages, *, model=None, system=None) -> str:
        self.answer_calls += 1
        self.last_prompt = "\n".join(m["content"] for m in messages)
        return self._scripted

    def list_models(self) -> List[str]:
        return ["stub-model"]


def _item(iid: str, why: str, text: str) -> Dict[str, Any]:
    return {"id": iid, "type": "note", "why": why, "locator": {}, "text": text,
            "preview": text, "pointer_eligible": False}


def _card_with_two_items(order=("i1", "i2")) -> Dict[str, Any]:
    """A card whose ``rendered_section`` lays its two items out CONTIGUOUSLY (the same shape
    ``file_context_store``/``card_content_render`` produce): "### Card\\n...\\nContent:\\n<items>"."""
    by_id = {
        "i1": _item("i1", "first", "First item text"),
        "i2": _item("i2", "second", "Second item text"),
    }
    ordered = [by_id[i] for i in order]
    body = "\n".join("\n".join(render_block_lines(it)) for it in ordered)
    rendered_section = f"### Card\nSummary line\n\nContent:\n{body}"
    return {
        "id": "card-1", "title": "Card", "adapter": "keyword", "relevance_score": 0.9,
        "files": [], "items": ordered, "rendered_section": rendered_section,
    }


def _hybrid(card: Dict[str, Any], provider: _ScriptedProvider) -> HybridContextAssembler:
    return HybridContextAssembler(
        _FixedAssembler([card]), _EmptyAssembler(),
        model_provider=provider, model="stub-model",
    )


# ---------------------------------------------------------------------------
# The hint rides the consolidation prompt.
# ---------------------------------------------------------------------------


def test_recent_item_usage_hint_appears_in_consolidation_prompt():
    card = _card_with_two_items()
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i2", "deliver": "paste"},'
        ' {"item_id": "i1", "deliver": "paste"}]}]}'
    )
    _hybrid(card, provider).assemble(
        "do the thing", meta={"recent_item_usage": {"card-1": ["i2", "i1"]}})
    assert "recently useful for a similar input: i2, i1" in provider.last_prompt


def test_no_hint_line_when_meta_has_no_recent_item_usage():
    # The generic RULE about recent_item_usage always appears in the prompt (a static instruction);
    # what must NOT appear absent a hint is the PER-CARD marker line naming actual item ids.
    card = _card_with_two_items()
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i1", "deliver": "paste"}]}]}'
    )
    _hybrid(card, provider).assemble("do the thing")
    assert "(recently useful for a similar input:" not in provider.last_prompt


def test_hint_ids_not_on_the_card_are_filtered_out_of_the_prompt():
    # recent_item_usage names a card the current candidate set doesn't even carry ids for, plus an
    # id ("i9") that isn't one of card-1's known items -- both are silently dropped, never crash.
    card = _card_with_two_items()
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i1", "deliver": "paste"}]}]}'
    )
    _hybrid(card, provider).assemble(
        "do the thing",
        meta={"recent_item_usage": {"card-1": ["i9"], "unknown-card": ["z1"]}},
    )
    assert "(recently useful for a similar input:" not in provider.last_prompt


# ---------------------------------------------------------------------------
# The consolidator's returned item ORDER reaches the rebuilt rendered_section, not just the
# returned ``items`` list.
# ---------------------------------------------------------------------------


def test_consolidator_item_reorder_reflected_in_rendered_section():
    # Card's ORIGINAL order is i1, i2; the consolidator (as if steered by a recent_item_usage hint)
    # returns i2 before i1. The rebuilt rendered_section must show i2's fragment BEFORE i1's.
    card = _card_with_two_items(order=("i1", "i2"))
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i2", "deliver": "paste"},'
        ' {"item_id": "i1", "deliver": "paste"}]}]}'
    )
    result = _hybrid(card, provider).assemble(
        "do the thing", meta={"recent_item_usage": {"card-1": ["i2", "i1"]}})
    section = result.card_metadata[0]["rendered_section"]
    assert section.index("Second item text") < section.index("First item text")
    assert [it["id"] for it in result.card_metadata[0]["items"]] == ["i2", "i1"]


def test_consolidator_same_order_leaves_section_untouched():
    card = _card_with_two_items(order=("i1", "i2"))
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i1", "deliver": "paste"},'
        ' {"item_id": "i2", "deliver": "paste"}]}]}'
    )
    result = _hybrid(card, provider).assemble("do the thing")
    section = result.card_metadata[0]["rendered_section"]
    assert section == card["rendered_section"]


def test_consolidator_prune_still_removes_dropped_item_from_rendered_section():
    card = _card_with_two_items()
    provider = _ScriptedProvider(
        '{"cards": [{"card_id": "card-1", "items": [{"item_id": "i1", "deliver": "paste"}]}]}'
    )
    result = _hybrid(card, provider).assemble("do the thing")
    section = result.card_metadata[0]["rendered_section"]
    assert "First item text" in section
    assert "Second item text" not in section
