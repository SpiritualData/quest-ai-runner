"""Offline tests for the consolidating holistic context filter (``consolidate_context``).

After both retrieval arms each filter their own cards, the hybrid runs ONE consolidating LLM pass
over the MERGED card set: it drops tangential/redundant cards across arms, reranks them, and prunes
which content ITEMS inside each kept card survive. The LLM selects ids only (content stays VERBATIM),
so these tests drive it with a stub provider returning scripted JSON, no network and no real model.

The data contract under test (the consolidator OUTPUT):
    [ {"card_id": str, "items": [{"item_id": str, "deliver": "paste"|"pointer"}]} ]
Graceful fallback (no provider / parse fail): keep ALL cards + ALL items, deliver="paste".
"""
from __future__ import annotations

from typing import Any, Dict, List

from quest_ai_runner.core.card_filter import consolidate_context


# ---------------------------------------------------------------------------
# Stub provider: replays a scripted JSON answer (the consolidator verdict).
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """A ModelProvider whose ``answer()`` returns a fixed scripted string."""

    def __init__(self, scripted: str):
        self._scripted = scripted
        self.answer_calls = 0
        self.last_prompt = ""

    def plan(self, prompt, *, model, tool_schema):  # pragma: no cover - unused here
        return {"action": "answer"}

    def answer(self, messages, *, model=None, system=None) -> str:
        self.answer_calls += 1
        self.last_prompt = "\n".join(m["content"] for m in messages)
        return self._scripted

    def list_models(self) -> List[str]:
        return ["stub-model"]


def _cards() -> List[Dict[str, Any]]:
    """Two cards; the first has two items, the second has one."""
    return [
        {
            "id": "card-a",
            "title": "Billing pipeline",
            "items": [
                {"id": "a1", "type": "file", "why": "entry point", "preview": "def run(): ..."},
                {"id": "a2", "type": "note", "why": "redundant", "preview": "old note"},
            ],
        },
        {
            "id": "card-b",
            "title": "Unrelated UI theme",
            "items": [
                {"id": "b1", "type": "collection", "why": "palette", "preview": "colors"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# No provider -> graceful keep-all fallback (the never-worse guarantee).
# ---------------------------------------------------------------------------

class TestNoProviderFallback:
    def test_no_provider_keeps_all_cards_and_items_paste(self):
        out = consolidate_context("anything", _cards(), model_provider=None)
        assert [c["card_id"] for c in out] == ["card-a", "card-b"]
        assert [i["item_id"] for i in out[0]["items"]] == ["a1", "a2"]
        assert all(i["deliver"] == "paste" for c in out for i in c["items"])

    def test_empty_cards_returns_empty(self):
        assert consolidate_context("x", [], model_provider=None) == []
        assert consolidate_context("x", [], model_provider=_ScriptedProvider("[]")) == []

    def test_parse_failure_falls_back_to_keep_all(self):
        prov = _ScriptedProvider("this is not json at all")
        out = consolidate_context("task", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-a", "card-b"]
        assert all(i["deliver"] == "paste" for c in out for i in c["items"])

    def test_empty_valid_verdict_falls_back_to_keep_all(self):
        # A valid-but-empty array would drop ALL context; never-worse -> keep all.
        prov = _ScriptedProvider("[]")
        out = consolidate_context("task", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-a", "card-b"]


# ---------------------------------------------------------------------------
# Keep / drop / rerank / item-prune per the stub verdict.
# ---------------------------------------------------------------------------

class TestConsolidatorVerdict:
    def test_drops_a_tangential_card(self):
        # Keep only card-a (its first item); card-b is dropped entirely.
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "card-a", "items": [{"item_id": "a1", "deliver": "paste"}]}]}'
        )
        out = consolidate_context("billing", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-a"]
        assert [i["item_id"] for i in out[0]["items"]] == ["a1"]
        assert prov.answer_calls == 1  # exactly ONE LLM call

    def test_reranks_cards_into_returned_order(self):
        # Put card-b first, then card-a -> the output order follows the verdict, not the input.
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "card-b", "items": [{"item_id": "b1", "deliver": "paste"}]},'
            ' {"card_id": "card-a", "items": [{"item_id": "a1", "deliver": "paste"}]}]}'
        )
        out = consolidate_context("theme", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-b", "card-a"]

    def test_prunes_redundant_item_within_a_card(self):
        # Keep card-a but only item a1 (drop the redundant a2).
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "card-a", "items": [{"item_id": "a1", "deliver": "pointer"}]}]}'
        )
        out = consolidate_context("billing", _cards(), model_provider=prov)
        assert [i["item_id"] for i in out[0]["items"]] == ["a1"]
        # A file item may be delivered as a pointer.
        assert out[0]["items"][0]["deliver"] == "pointer"

    def test_unknown_card_and_item_ids_are_ignored(self):
        # Hallucinated card-z and item a9 are dropped; only the real a1 survives.
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "card-z", "items": [{"item_id": "x", "deliver": "paste"}]},'
            ' {"card_id": "card-a", "items": [{"item_id": "a9", "deliver": "paste"},'
            ' {"item_id": "a1", "deliver": "paste"}]}]}'
        )
        out = consolidate_context("billing", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-a"]
        assert [i["item_id"] for i in out[0]["items"]] == ["a1"]

    def test_deliver_defaults_to_paste_when_unknown(self):
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "card-a", "items": [{"item_id": "a1", "deliver": "weird"}]}]}'
        )
        out = consolidate_context("billing", _cards(), model_provider=prov)
        assert out[0]["items"][0]["deliver"] == "paste"

    def test_fenced_json_is_parsed(self):
        prov = _ScriptedProvider(
            '```json\n{"cards": [{"card_id": "card-a", "items": '
            '[{"item_id": "a1", "deliver": "paste"}]}]}\n```'
        )
        out = consolidate_context("billing", _cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["card-a"]


class TestFileOnlyCards:
    """A card with NO items (a file/reference card) is a valid candidate: the LLM keeps or drops it
    at the CARD level, and a kept one comes back with an empty items list (keep the whole card)."""

    def _mixed_cards(self) -> List[Dict[str, Any]]:
        return [
            {"id": "items-card", "title": "Has items",
             "items": [{"id": "i1", "type": "note", "why": "w", "preview": "p"}]},
            {"id": "file-only", "title": "File listing card", "items": [],
             "preview": "summary + file listings"},
        ]

    def test_file_only_card_can_be_kept_whole(self):
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "items-card", "items": [{"item_id": "i1", "deliver": "paste"}]},'
            ' {"card_id": "file-only", "items": []}]}'
        )
        out = consolidate_context("task", self._mixed_cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["items-card", "file-only"]
        # The file-only card is kept with an empty item list (nothing to prune).
        assert out[1]["items"] == []

    def test_file_only_card_can_be_dropped(self):
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "items-card", "items": [{"item_id": "i1", "deliver": "paste"}]}]}'
        )
        out = consolidate_context("task", self._mixed_cards(), model_provider=prov)
        assert [c["card_id"] for c in out] == ["items-card"]

    def test_item_bearing_card_pruned_to_zero_items_is_dropped(self):
        # Distinguish: an ITEM-BEARING card whose every item is pruned IS dropped (not kept whole).
        prov = _ScriptedProvider(
            '{"cards": [{"card_id": "items-card", "items": []},'
            ' {"card_id": "file-only", "items": []}]}'
        )
        out = consolidate_context("task", self._mixed_cards(), model_provider=prov)
        # items-card had items but none survived -> dropped; file-only kept whole.
        assert [c["card_id"] for c in out] == ["file-only"]
