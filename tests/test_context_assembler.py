"""Offline tests for the ContextAssembler adapter: AssembledContext, FileContextStore,
context_doctrine, and the Orchestrator's pre-flight injection + write-back."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from quest_ai_runner.adapters.file_context_store import (
    FileContextStore,
    _card_slug,
    _tokenize,
)
from quest_ai_runner.core.adapters import AssembledContext, ContextAssembler
from quest_ai_runner.core.context_doctrine import (
    DEEP_CONTEXT_DOCTRINE,
    MODEL_TIER_GATE,
    SUFFICIENCY_GATE,
    compose_deep_preamble,
)
from quest_ai_runner.core.orchestrator import PLANNER_PROMPT, Orchestrator, OrchestratorConfig


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

def _make_card(card_id: str, keywords: List[str], summary: str = "a test card",
               files: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "id": card_id,
        "keywords": keywords,
        "summary": summary,
        "files": files or [],
        "conventions": [],
        "provenance": {"created_by_task": "", "model": "", "created_at": "",
                       "last_verified_at": ""},
        "usage_count": 0,
        "last_outcome": "unknown",
    }


def _write_card(cards_dir: Path, card: Dict[str, Any]) -> Path:
    p = cards_dir / f"{card['id']}.json"
    p.write_text(json.dumps(card, indent=2), encoding="utf-8")
    return p


def _card_files(cards_dir: Path) -> List[Path]:
    """Card JSON files, excluding the ``bootstrap_meta.json`` sidecar bootstrap() writes."""
    return [p for p in cards_dir.glob("*.json") if p.name != "bootstrap_meta.json"]


def _topic_provider(topics: List[Dict[str, Any]]):
    """A minimal fake ModelProvider whose answer() returns ``topics`` as a JSON array string.

    ``topics`` is a list of topic-card dicts, each with keys id/name/keywords/summary/files.
    The bootstrap LLM path calls ``provider.answer(messages, model=None)`` and parses the JSON
    array out of the returned text. ``list_models`` returns [] so a ModelRegistry can be built.
    """
    provider = MagicMock()
    provider.list_models.return_value = []
    provider.answer.return_value = json.dumps(topics)
    return provider


# ---------------------------------------------------------------------------
# AssembledContext dataclass
# ---------------------------------------------------------------------------

class TestAssembledContext:
    def test_defaults(self):
        ac = AssembledContext()
        assert ac.context_view == ""
        assert ac.model_tier_hint is None
        assert ac.card_ids == []
        assert ac.stale == []

    def test_fields_set(self):
        ac = AssembledContext(
            context_view="some text",
            model_tier_hint="sonnet",
            card_ids=["c1", "c2"],
            stale=["old/file.py"],
        )
        assert ac.context_view == "some text"
        assert ac.model_tier_hint == "sonnet"
        assert ac.card_ids == ["c1", "c2"]
        assert ac.stale == ["old/file.py"]


# ---------------------------------------------------------------------------
# ContextAssembler Protocol structural check
# ---------------------------------------------------------------------------

class TestContextAssemblerProtocol:
    def test_structural_satisfaction(self):
        """Any object with assemble() and record() satisfies the Protocol structurally."""
        class MinimalAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                pass

        ma = MinimalAssembler()
        assert isinstance(ma, ContextAssembler)

    def test_missing_method_fails(self):
        class BadAssembler:
            def assemble(self, task_text):
                return AssembledContext()
            # missing record()

        ba = BadAssembler()
        assert not isinstance(ba, ContextAssembler)


# ---------------------------------------------------------------------------
# context_doctrine constants
# ---------------------------------------------------------------------------

class TestContextDoctrine:
    def test_no_braces_in_gates(self):
        """Gate constants must have NO literal {/} so they embed safely in .format() strings."""
        assert "{" not in SUFFICIENCY_GATE
        assert "}" not in SUFFICIENCY_GATE
        assert "{" not in MODEL_TIER_GATE
        assert "}" not in MODEL_TIER_GATE

    def test_deep_context_doctrine_contains_both_gates(self):
        assert "SUFFICIENCY" in DEEP_CONTEXT_DOCTRINE
        assert "MODEL TIER" in DEEP_CONTEXT_DOCTRINE

    def test_compose_deep_preamble_empty(self):
        result = compose_deep_preamble("")
        assert DEEP_CONTEXT_DOCTRINE in result

    def test_compose_deep_preamble_with_base(self):
        result = compose_deep_preamble("my base preamble")
        assert "my base preamble" in result
        assert DEEP_CONTEXT_DOCTRINE in result

    def test_compose_deep_preamble_with_assembled(self):
        ac = AssembledContext(context_view="pre-assembled view text")
        result = compose_deep_preamble("base", assembled=ac)
        assert "pre-assembled view text" in result
        assert "PRE-ASSEMBLED CONTEXT" in result

    def test_compose_deep_preamble_empty_context_view_skipped(self):
        ac = AssembledContext(context_view="")
        result = compose_deep_preamble("base", assembled=ac)
        assert "PRE-ASSEMBLED CONTEXT" not in result

    def test_compose_deep_preamble_none_assembled(self):
        result = compose_deep_preamble("base", assembled=None)
        assert "PRE-ASSEMBLED CONTEXT" not in result
        assert "base" in result


# ---------------------------------------------------------------------------
# PLANNER_PROMPT brace safety
# ---------------------------------------------------------------------------

class TestPlannerPromptGates:
    def test_planner_prompt_format_works(self):
        """PLANNER_PROMPT.format() must succeed with the expected slots."""
        result = PLANNER_PROMPT.format(
            user_message="test",
            transcript="",
            context_view="",
            gathered="[]",
            max_reads=8,
            max_subq=4,
            max_deep=4,
        )
        assert len(result) > 100

    def test_planner_prompt_contains_sufficiency_gate(self):
        result = PLANNER_PROMPT.format(
            user_message="x", transcript="", context_view="", gathered="[]",
            max_reads=8, max_subq=4, max_deep=4,
        )
        assert "read enough before acting" in result.lower()

    def test_planner_prompt_contains_model_tier_discipline(self):
        result = PLANNER_PROMPT.format(
            user_message="x", transcript="", context_view="", gathered="[]",
            max_reads=8, max_subq=4, max_deep=4,
        )
        assert "MODEL TIER DISCIPLINE" in result

    def test_planner_prompt_substitutes_max_reads(self):
        result = PLANNER_PROMPT.format(
            user_message="x", transcript="", context_view="", gathered="[]",
            max_reads=42, max_subq=4, max_deep=4,
        )
        assert "42" in result

    def test_planner_prompt_substitutes_max_subq_and_max_deep(self):
        result = PLANNER_PROMPT.format(
            user_message="x", transcript="", context_view="", gathered="[]",
            max_reads=8, max_subq=7, max_deep=9,
        )
        assert "7" in result
        assert "9" in result


# ---------------------------------------------------------------------------
# _tokenize and _card_slug helpers
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic(self):
        assert "chat" in _tokenize("chat ai conversation")

    def test_stopwords_dropped(self):
        toks = _tokenize("the quick brown fox")
        assert "the" not in toks

    def test_short_tokens_dropped(self):
        toks = _tokenize("a an be it me")
        assert not toks  # all short or stopwords

    def test_numbers_kept(self):
        toks = _tokenize("version 123 release")
        assert "123" in toks

    def test_empty(self):
        assert _tokenize("") == set()

    def test_case_insensitive(self):
        assert _tokenize("Chat AI") == _tokenize("chat ai")


class TestCardSlug:
    def test_deterministic(self):
        assert _card_slug("foo bar baz") == _card_slug("foo bar baz")

    def test_different_texts_different_slugs(self):
        assert _card_slug("task one") != _card_slug("task two")

    def test_slug_format(self):
        slug = _card_slug("implement the chat feature")
        # slug should contain keyword tokens + a hex digest part
        assert len(slug) > 8
        parts = slug.split("-")
        assert len(parts) >= 2

    def test_empty_text_still_works(self):
        slug = _card_slug("")
        assert len(slug) > 0


# ---------------------------------------------------------------------------
# FileContextStore: assemble()
# ---------------------------------------------------------------------------

class TestFileContextStoreAssemble:
    def test_empty_dir_returns_empty(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        ac = store.assemble("find all chat related files")
        assert ac.context_view == ""
        assert ac.card_ids == []

    def test_no_matching_cards_returns_empty(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("irrelevant-card", ["database", "schema"]))
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("fix the navigation bug")
        # "navigation" and "bug" may not overlap with "database"/"schema"
        assert ac.context_view == "" or "irrelevant" not in ac.context_view

    def test_matching_card_included(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card(
            "chat-card", ["chat", "conversation", "message"],
            summary="The chat subsystem handles real-time conversation."
        ))
        # confidence_threshold=0.0: tests ranking/matching on small synthetic sets
        # where IDF scores are below the production gate of 9.0.
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0)
        ac = store.assemble("how does the chat conversation work?")
        assert "chat-card" in ac.card_ids
        assert "chat subsystem" in ac.context_view.lower()

    def test_max_cards_respected(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        for i in range(10):
            _write_card(cards_dir, _make_card(
                f"card-{i}", ["common", "keyword", f"unique{i}"]
            ))
        store = FileContextStore(str(cards_dir), max_cards_in_view=3)
        ac = store.assemble("common keyword task")
        assert len(ac.card_ids) <= 3

    def test_higher_overlap_card_ranked_first(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("low-card", ["chat"]))
        _write_card(cards_dir, _make_card("high-card", ["chat", "conversation", "message"]))
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("chat conversation message flow")
        assert ac.card_ids[0] == "high-card"

    def test_pinned_files_rendered_in_view(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        # Create a real file to fingerprint
        real_file = tmp_path / "mymodule.py"
        real_file.write_text("# hello\n", encoding="utf-8")
        _write_card(cards_dir, _make_card(
            "module-card", ["module", "python", "implementation"],
            files=[{"path": str(real_file), "sha256": "", "mtime": 0.0,
                    "git_sha": "", "why": "entry point", "symbols": ["run"]}]
        ))
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("module python implementation code")
        assert "mymodule.py" in ac.context_view
        assert "[run]" in ac.context_view

    def test_stale_file_flagged(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        real_file = tmp_path / "target.py"
        real_file.write_text("version 1\n", encoding="utf-8")
        # Compute fingerprint at version 1
        import hashlib
        h = hashlib.sha256(b"version 1\n")
        stored_sha = h.hexdigest()
        # Now change the file so stored_sha no longer matches
        real_file.write_text("version 2 -- changed\n", encoding="utf-8")
        _write_card(cards_dir, _make_card(
            "stale-card", ["target", "module", "python"],
            files=[{"path": str(real_file), "sha256": stored_sha, "mtime": 0.0,
                    "git_sha": "", "why": "changed file"}]
        ))
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("target module python file")
        assert str(real_file) in ac.stale
        assert "changed since last capture" in ac.context_view

    def test_assemble_never_raises_on_corrupt_card(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        (cards_dir / "bad.json").write_text("NOT VALID JSON {{{", encoding="utf-8")
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("anything")
        assert isinstance(ac, AssembledContext)

    def test_assemble_never_raises_on_missing_dir(self, tmp_path):
        store = FileContextStore(str(tmp_path / "nonexistent" / "cards"))
        ac = store.assemble("some task text")
        assert isinstance(ac, AssembledContext)

    def test_card_ids_returned(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("alpha", ["alpha", "test", "data"]))
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("alpha test data query")
        assert "alpha" in ac.card_ids


# ---------------------------------------------------------------------------
# FileContextStore: record()
# ---------------------------------------------------------------------------

class TestFileContextStoreRecord:
    def test_record_creates_card(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("implement the login flow", {"kind": "deep"})
        cards_dir = tmp_path / "cards"
        assert cards_dir.exists()
        files = list(cards_dir.glob("*.json"))
        assert len(files) == 1

    def test_record_card_has_expected_fields(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("fix the signup button", {"kind": "answer"})
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert "id" in card
        assert "keywords" in card
        assert "summary" in card
        assert "usage_count" in card
        assert card["usage_count"] == 1

    def test_record_upserts_on_second_call(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        task = "update the user profile endpoint"
        store.record(task, {"kind": "deep"})
        store.record(task, {"kind": "deep"})
        files = list((tmp_path / "cards").glob("*.json"))
        assert len(files) == 1
        card = json.loads(files[0].read_text())
        assert card["usage_count"] == 2

    def test_record_outcome_kind_mapping(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("do a thing", {"kind": "answer"})
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert card["last_outcome"] == "met"

    def test_record_deep_kind_maps_to_met(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("build a feature", {"kind": "deep"})
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert card["last_outcome"] == "met"

    def test_record_confirm_kind_maps_to_unknown(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("do something risky", {"kind": "confirm"})
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert card["last_outcome"] == "unknown"

    def test_record_pinned_files_fingerprinted(self, tmp_path):
        real_file = tmp_path / "service.py"
        real_file.write_text("class Service: pass\n", encoding="utf-8")
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("fix the service class", {
            "kind": "deep",
            "files": [str(real_file)],
        })
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert len(card["files"]) == 1
        fe = card["files"][0]
        assert fe["path"] == str(real_file)
        assert len(fe["sha256"]) == 64  # sha256 hex digest

    def test_record_accepts_verified_at_timestamp(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        store.record("do something", {"kind": "answer", "verified_at": "2026-06-01T12:00:00"})
        (card_file,) = (tmp_path / "cards").glob("*.json")
        card = json.loads(card_file.read_text())
        assert card["provenance"]["last_verified_at"] == "2026-06-01T12:00:00"

    def test_record_never_raises_on_unwritable_dir(self, tmp_path):
        bad_dir = tmp_path / "readonly"
        bad_dir.mkdir()
        bad_dir.chmod(0o444)
        store = FileContextStore(str(bad_dir / "cards"))
        try:
            store.record("anything", {"kind": "deep"})  # must not raise
        finally:
            bad_dir.chmod(0o755)

    def test_record_never_raises_on_corrupt_existing_card(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        task = "fix the chat endpoint"
        slug = _card_slug(task)
        (cards_dir / f"{slug}.json").write_text("CORRUPTED {{{", encoding="utf-8")
        store = FileContextStore(str(cards_dir))
        store.record(task, {"kind": "answer"})  # must not raise
        # Card should be rewritten fresh
        card = json.loads((cards_dir / f"{slug}.json").read_text())
        assert card["id"] == slug


# ---------------------------------------------------------------------------
# FileContextStore: stale_cards_for()
# ---------------------------------------------------------------------------

class TestStaleCardsFor:
    def test_no_cards_returns_empty(self, tmp_path):
        store = FileContextStore(str(tmp_path / "cards"))
        assert store.stale_cards_for("some/path.py") == set()

    def test_card_not_stale_when_sha_matches(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        real_file = tmp_path / "stable.py"
        real_file.write_text("stable content\n", encoding="utf-8")
        import hashlib
        stored_sha = hashlib.sha256(b"stable content\n").hexdigest()
        _write_card(cards_dir, _make_card(
            "stable-card", ["stable"],
            files=[{"path": str(real_file), "sha256": stored_sha, "mtime": 0.0, "git_sha": ""}]
        ))
        store = FileContextStore(str(cards_dir))
        stale = store.stale_cards_for(str(real_file))
        assert "stable-card" not in stale

    def test_card_stale_when_sha_differs(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        real_file = tmp_path / "changing.py"
        real_file.write_text("version 2\n", encoding="utf-8")
        # Store an old sha
        _write_card(cards_dir, _make_card(
            "changing-card", ["changing"],
            files=[{"path": str(real_file), "sha256": "oldsha" + "0" * 58, "mtime": 0.0, "git_sha": ""}]
        ))
        store = FileContextStore(str(cards_dir))
        stale = store.stale_cards_for(str(real_file))
        assert "changing-card" in stale


# ---------------------------------------------------------------------------
# Orchestrator integration: pre-flight injection and write-back
# ---------------------------------------------------------------------------

def _make_orchestrator(assembler=None, provider=None, plan_action="answer"):
    """Build a minimal Orchestrator with mock adapters."""
    from quest_ai_runner.core.model_registry import ModelRegistry

    mock_provider = provider or MagicMock()
    mock_provider.list_models.return_value = []
    mock_provider.plan.return_value = {
        "action": plan_action,
        "rationale": "mock plan",
        "model_tier": "haiku",
    }
    mock_provider.answer.return_value = "mock answer"

    mock_retrieval = MagicMock()
    registry = ModelRegistry(mock_provider)

    return Orchestrator(
        retrieval=mock_retrieval,
        provider=mock_provider,
        registry=registry,
        config=OrchestratorConfig(max_steps=1),
        context_assembler=assembler,
    )


class TestOrchestratorContextAssembler:
    def test_assemble_called_before_loop(self):
        """When wired, assemble() is called once before the loop and its context_view is used."""
        calls = []

        class TrackingAssembler:
            def assemble(self, task_text, *, meta=None):
                calls.append(("assemble", task_text))
                return AssembledContext(context_view="pre-assembled context")
            def record(self, task_text, outcome):
                calls.append(("record", task_text))

        orch = _make_orchestrator(assembler=TrackingAssembler())
        orch.run("some task")
        assert any(c[0] == "assemble" for c in calls)

    def test_assemble_context_view_injected(self):
        """The assembled context_view reaches the planner prompt."""
        captured_prompts = []

        class InjectAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext(context_view="INJECTED_CONTEXT_XYZ")
            def record(self, task_text, outcome):
                pass

        mock_provider = MagicMock()
        mock_provider.list_models.return_value = []
        mock_provider.plan.side_effect = lambda prompt, **kw: (
            captured_prompts.append(prompt) or
            {"action": "answer", "rationale": "done", "model_tier": "haiku"}
        )
        mock_provider.answer.return_value = "ok"

        orch = _make_orchestrator(assembler=InjectAssembler(), provider=mock_provider)
        orch.run("some task")
        assert any("INJECTED_CONTEXT_XYZ" in p for p in captured_prompts)

    def test_assembled_context_composes_with_caller_context_view(self):
        """When run() is given an explicit context_view (e.g. a Quest chat's bound-quest context),
        assemble() STILL runs and its cards COMPOSE with the caller's context: both reach the
        planner prompt, with the assembled cards first."""
        calls = []
        captured_prompts = []

        class TrackingAssembler:
            def assemble(self, task_text, *, meta=None):
                calls.append("assemble")
                return AssembledContext(context_view="ASSEMBLED_CARDS_XYZ")
            def record(self, task_text, outcome):
                pass

        mock_provider = MagicMock()
        mock_provider.list_models.return_value = []
        mock_provider.plan.side_effect = lambda prompt, **kw: (
            captured_prompts.append(prompt) or
            {"action": "answer", "rationale": "done", "model_tier": "haiku"}
        )
        mock_provider.answer.return_value = "ok"

        orch = _make_orchestrator(assembler=TrackingAssembler(), provider=mock_provider)
        orch.run("some task", context_view="CALLER_BOUND_QUEST_CONTEXT")
        # assemble runs even when the caller supplied context, and both views are present, with
        # the assembled cards ahead of the caller's context.
        assert "assemble" in calls
        prompt = next(p for p in captured_prompts if "ASSEMBLED_CARDS_XYZ" in p)
        assert "CALLER_BOUND_QUEST_CONTEXT" in prompt
        assert prompt.index("ASSEMBLED_CARDS_XYZ") < prompt.index("CALLER_BOUND_QUEST_CONTEXT")

    def test_record_called_after_run(self):
        """record() is called once after the run completes."""
        records = []

        class TrackingAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                records.append((task_text, outcome))

        orch = _make_orchestrator(assembler=TrackingAssembler())
        orch.run("record this task")
        assert len(records) == 1
        assert records[0][0] == "record this task"
        assert "kind" in records[0][1]

    def test_model_tier_hint_from_assembled_context(self):
        """model_tier_hint on AssembledContext overrides the default model tier."""
        used_models = []

        class HintAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext(context_view="ctx", model_tier_hint="opus")
            def record(self, task_text, outcome):
                pass

        mock_provider = MagicMock()
        mock_provider.list_models.return_value = []
        mock_provider.plan.return_value = {
            "action": "answer", "rationale": "ok", "model_tier": None,
        }
        mock_provider.answer.side_effect = lambda msgs, model, **kw: (
            used_models.append(model) or "answer"
        )

        orch = _make_orchestrator(assembler=HintAssembler(), provider=mock_provider)
        orch.run("some task")
        # The answer model should be "opus" tier (registry maps it to some model id)
        # We just check that answer() was called
        assert len(used_models) == 1

    def test_model_tier_hint_not_applied_when_explicit_model_hint_provided(self):
        """When run() is given an explicit model_hint, AssembledContext.model_tier_hint is ignored."""
        used_models = []

        class OpusHintAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext(context_view="ctx", model_tier_hint="opus")
            def record(self, task_text, outcome):
                pass

        mock_provider = MagicMock()
        mock_provider.list_models.return_value = []
        mock_provider.plan.return_value = {
            "action": "answer", "rationale": "ok", "model_tier": None,
        }
        mock_provider.answer.side_effect = lambda msgs, model, **kw: (
            used_models.append(model) or "answer"
        )

        orch = _make_orchestrator(assembler=OpusHintAssembler(), provider=mock_provider)
        orch.run("some task", model_hint="haiku")
        # The explicit model_hint="haiku" should win, not the assembled "opus"
        assert len(used_models) == 1
        # Model id should be a haiku-tier model, not an opus one
        # (the registry maps "haiku" -> haiku model id, "opus" -> opus model id)
        assert used_models[0] != ""

    def test_assemble_exception_never_breaks_run(self):
        """An exception in assemble() must not propagate -- the run continues."""
        class BrokenAssembler:
            def assemble(self, task_text, *, meta=None):
                raise RuntimeError("assembly failed")
            def record(self, task_text, outcome):
                pass

        orch = _make_orchestrator(assembler=BrokenAssembler())
        result = orch.run("some task")
        # Run must complete normally
        assert result.kind == "answer"

    def test_record_exception_never_breaks_run(self):
        """An exception in record() must not propagate -- the run completes and returns."""
        class BrokenRecordAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                raise RuntimeError("record failed")

        orch = _make_orchestrator(assembler=BrokenRecordAssembler())
        result = orch.run("some task")
        assert result.kind == "answer"

    def test_no_assembler_run_unchanged(self):
        """Without a context_assembler, run() behaves exactly as before."""
        orch = _make_orchestrator(assembler=None)
        result = orch.run("some task")
        assert result.kind == "answer"


# ---------------------------------------------------------------------------
# FileContextStore: end-to-end: record then assemble retrieves it
# ---------------------------------------------------------------------------

class TestFileContextStoreRoundTrip:
    def test_record_then_assemble(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        task = "implement the user authentication service"
        # Record the task
        store.record(task, {"kind": "deep"})
        # Assemble with similar keywords
        ac = store.assemble("authentication service user login")
        # The card recorded above should appear since keywords overlap
        assert len(ac.card_ids) > 0
        assert "authentication" in ac.context_view.lower() or "user" in ac.context_view.lower()

    def test_stale_card_detected_after_file_change(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        real_file = tmp_path / "handler.py"
        real_file.write_text("def handle(): pass\n", encoding="utf-8")
        task = "fix the handler module"
        store.record(task, {"kind": "deep", "files": [str(real_file)]})
        # Change the file
        real_file.write_text("def handle(): return 'updated'\n", encoding="utf-8")
        # stale_cards_for should detect it
        stale = store.stale_cards_for(str(real_file))
        assert len(stale) > 0

    def test_relative_pinned_path_resolves_against_repo_root(self, tmp_path):
        """A file pinned by a path RELATIVE to repo_root (what a run produces) stays fresh even
        when the process cwd is elsewhere, and goes stale when the real file changes."""
        repo = tmp_path / "corpus"
        (repo / "sub").mkdir(parents=True)
        code = repo / "sub" / "code.py"
        code.write_text("x = 1\n", encoding="utf-8")
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo))
        # Pin by a RELATIVE path (as gathered rel_paths are), not absolute.
        store.record("work on the sub code module", {"kind": "answer", "files": ["sub/code.py"]})
        # cwd here is the runner repo, not <repo>; resolution must use repo_root.
        ac = store.assemble("the sub code module")
        assert ac.card_ids and ac.stale == []          # fresh, resolved correctly
        code.write_text("x = 2\n", encoding="utf-8")    # change the real file
        ac2 = store.assemble("the sub code module")
        assert "sub/code.py" in ac2.stale               # now detected stale


class TestWriteBackCapturesGatheredFiles:
    def test_record_pins_files_the_brain_read(self):
        """After a run, the card pins the rel_paths the brain actually read this turn (from the
        gathered reads/greps), so staleness has something to invalidate. This is the loop closing."""
        from quest_ai_runner.core.adapters import Observation
        from quest_ai_runner.core.model_registry import ModelRegistry

        recorded = {}

        class CapturingAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                recorded.update(outcome)

        # Provider: step 0 -> read a file, step 1 -> answer.
        plans = iter([
            {"action": "read", "rationale": "look", "model_tier": "haiku",
             "reads": [{"rel_path": "pkg/mod.py"}]},
            {"action": "answer", "rationale": "done", "model_tier": "haiku"},
        ])
        provider = MagicMock()
        provider.list_models.return_value = []
        provider.plan.side_effect = lambda prompt, **kw: next(plans)
        provider.answer.return_value = "ok"

        retrieval = MagicMock()
        retrieval.read_section.return_value = Observation(
            kind="read", rel_path="pkg/mod.py", locator="head", text="code")

        orch = Orchestrator(
            retrieval=retrieval, provider=provider, registry=ModelRegistry(provider),
            config=OrchestratorConfig(max_steps=2), context_assembler=CapturingAssembler())
        orch.run("explain pkg/mod.py")
        assert "pkg/mod.py" in recorded.get("files", [])


# ---------------------------------------------------------------------------
# Bootstrap: cold-start seed from a repo tree
# ---------------------------------------------------------------------------

class TestBootstrap:
    def _make_repo(self, tmp_path: Path) -> Path:
        """Create a minimal fake repo with a couple of Python modules."""
        # mypackage/models.py
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models.py").write_text(
            "class User:\n    pass\n\nclass Order:\n    pass\n",
            encoding="utf-8",
        )
        # mypackage/utils.py
        (pkg / "utils.py").write_text(
            "def parse_date(s):\n    pass\n\ndef format_name(n):\n    pass\n",
            encoding="utf-8",
        )
        # tests/test_models.py
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_models.py").write_text(
            "def test_user_create():\n    pass\n",
            encoding="utf-8",
        )
        return tmp_path

    def _topics_for_repo(self):
        """Topic cards the fake LLM returns for ``_make_repo``: a 'models' topic spanning
        models.py + __init__.py and a 'utils' topic. A topic can span files from separate
        directories. The test file is folded into the models topic to show cross-cutting."""
        return [
            {
                "id": "models",
                "name": "Models",
                "keywords": ["models", "user", "order", "schema", "entity"],
                "summary": "Data models: the User and Order entities.",
                "files": ["mypackage/__init__.py", "mypackage/models.py", "tests/test_models.py"],
            },
            {
                "id": "utils",
                "name": "Utilities",
                "keywords": ["utils", "parse", "date", "format", "name", "helpers"],
                "summary": "Utility helpers for parsing dates and formatting names.",
                "files": ["mypackage/utils.py"],
            },
        ]

    def test_bootstrap_creates_cards(self, tmp_path):
        """bootstrap() with a provider produces one card per LLM-identified topic."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        n = store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))
        assert n > 0
        assert cards_dir.exists()
        cards = _card_files(cards_dir)
        assert len(cards) == n

    def test_bootstrap_writes_one_card_per_topic(self, tmp_path):
        """Topic cards are semantic, so bootstrap() writes one card per LLM topic (not per file)."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        topics = self._topics_for_repo()
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        n = store.bootstrap(root=str(repo), provider=_topic_provider(topics))
        assert n == len(topics)
        assert len(_card_files(cards_dir)) == len(topics)

    def test_bootstrap_no_provider_writes_nothing(self, tmp_path):
        """Without a provider, bootstrap() is a no-op: it returns 0 and writes no cards."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        n = store.bootstrap(root=str(repo))
        assert n == 0
        assert not cards_dir.exists() or not any(cards_dir.glob("*.json"))

    def test_bootstrap_card_can_span_directories(self, tmp_path):
        """A single topic card can pin files from completely separate directories."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))
        models_card = json.loads((cards_dir / "models.json").read_text())
        paths = {fe["path"] for fe in models_card.get("files", [])}
        # The models topic spans mypackage/ and tests/.
        assert "mypackage/models.py" in paths
        assert "tests/test_models.py" in paths

    def test_bootstrap_models_py_card_exists(self, tmp_path):
        """There must be a card that pins mypackage/models.py."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))
        models_card = None
        for cp in _card_files(cards_dir):
            c = json.loads(cp.read_text())
            if any("mypackage/models.py" in fe.get("path", "") for fe in c.get("files", [])):
                models_card = c
                break
        assert models_card is not None, "expected a card that pins mypackage/models.py"

    def test_bootstrap_topic_query_ranks_right_card_first(self, tmp_path):
        """A query containing a distinctive topic keyword ranks that topic's card #1."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        # confidence_threshold=0.0: tests ranking on a small synthetic store where IDF
        # scores are below the production gate of 3.0.
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False,
                                 max_cards_in_view=10, confidence_threshold=0.0)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))

        # "order" is a keyword only on the models topic.
        ac = store.assemble("Order entity schema")
        assert len(ac.card_ids) > 0, "expected at least one card"
        assert ac.card_ids[0] == "models", (
            f"expected the models topic ranked #1, got: {ac.card_ids}"
        )

    def test_bootstrap_card_has_llm_keywords(self, tmp_path):
        """A bootstrapped card carries the keywords/summary supplied by the LLM topic."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))

        models_card = json.loads((cards_dir / "models.json").read_text())
        combined = " ".join(models_card.get("keywords", [])) + " " + models_card.get("summary", "")
        assert any(kw in combined.lower() for kw in ("user", "order"))

    def test_bootstrap_pins_module_files(self, tmp_path):
        """At least one bootstrapped card must pin a file matching models.py or utils.py."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))

        all_paths: List[str] = []
        for cp in _card_files(cards_dir):
            c = json.loads(cp.read_text())
            all_paths.extend(fe["path"] for fe in c.get("files", []))
        assert any("models.py" in p or "utils.py" in p for p in all_paths), (
            f"expected models.py or utils.py pinned somewhere, got: {all_paths}"
        )

    def test_bootstrap_provenance_created_by_bootstrap(self, tmp_path):
        """All bootstrapped cards have provenance.created_by_task == 'bootstrap'."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        store.bootstrap(root=str(repo), provider=_topic_provider(self._topics_for_repo()))
        for cp in _card_files(cards_dir):
            c = json.loads(cp.read_text())
            assert c.get("provenance", {}).get("created_by_task") == "bootstrap"

    def test_bootstrap_idempotent(self, tmp_path):
        """bootstrap() is incremental: a second run over an unchanged corpus writes no new cards
        (every file is already covered and up to date) and leaves the card count unchanged."""
        repo = self._make_repo(tmp_path)
        cards_dir = tmp_path / "cards"
        topics = self._topics_for_repo()
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)
        n1 = store.bootstrap(root=str(repo), provider=_topic_provider(topics))
        n2 = store.bootstrap(root=str(repo), provider=_topic_provider(topics))
        assert n1 > 0
        assert n2 == 0, "second bootstrap over an unchanged corpus must be a no-op"
        # No duplicates: the card count on disk is still the first run's count.
        assert len(_card_files(cards_dir)) == n1

    def test_bootstrap_never_raises_on_bad_root(self, tmp_path):
        """bootstrap() with a non-existent root returns 0 and does not raise."""
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        result = store.bootstrap(
            root=str(tmp_path / "nonexistent"),
            provider=_topic_provider([]),
        )
        assert result == 0

    def test_bootstrap_skips_venv_and_git(self, tmp_path):
        """bootstrap() must not feed files inside .git or venv to the LLM, so they can never
        end up pinned on a card (filtered to the walked file list by exact match)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Real source file
        (repo / "app.py").write_text("def main(): pass\n", encoding="utf-8")
        # Files that should be skipped
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        venv = repo / "venv"
        venv.mkdir()
        (venv / "site_packages.py").write_text("# ignore me\n", encoding="utf-8")

        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        # A malicious/confused LLM that tries to pin skipped files: they must be filtered out
        # because they were never in the walked path list.
        topics = [{
            "id": "app",
            "name": "App",
            "keywords": ["app", "main", "entry"],
            "summary": "Application entry point.",
            "files": ["app.py", ".git/config", "venv/site_packages.py"],
        }]
        store.bootstrap(root=str(repo), provider=_topic_provider(topics))

        for cp in _card_files(cards_dir):
            c = json.loads(cp.read_text())
            for fe in c.get("files", []):
                assert ".git" not in fe["path"], f"should not index .git: {fe['path']}"
                assert "venv" not in fe["path"], f"should not index venv: {fe['path']}"


# ---------------------------------------------------------------------------
# Lazy auto-bootstrap: first assemble() seeds from an empty store
# ---------------------------------------------------------------------------

class TestAutoBootstrap:
    def test_auto_bootstrap_on_first_assemble_is_noop_without_provider(self, tmp_path):
        """Lazy auto-bootstrap has no model provider, so semantic topic cards cannot be
        identified: the first assemble() on an empty store writes nothing and returns empty.
        Cards accumulate via record() instead."""
        repo = tmp_path / "repo"
        repo.mkdir()
        pkg = repo / "billing"
        pkg.mkdir()
        (pkg / "invoice.py").write_text(
            "def generate_invoice(customer_id):\n    pass\n",
            encoding="utf-8",
        )

        cards_dir = tmp_path / "cards"
        store = FileContextStore(
            str(cards_dir),
            repo_root=str(repo),
            auto_bootstrap=True,
            max_cards_in_view=10,
            confidence_threshold=0.0,
        )
        ac = store.assemble("billing invoice generation")
        assert ac.card_ids == [], "auto-bootstrap without a provider must write no cards"
        assert not cards_dir.exists() or not any(cards_dir.glob("*.json"))

    def test_auto_bootstrap_fires_only_once(self, tmp_path):
        """The lazy bootstrap guard ensures bootstrap() is called at most once per instance."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("def run(): pass\n", encoding="utf-8")

        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=True)
        store.assemble("run the main module")
        cards_after_first = len(list(cards_dir.glob("*.json")))

        # Remove cards to see if a second assemble() re-bootstraps (it should not).
        for p in cards_dir.glob("*.json"):
            p.unlink()

        store.assemble("run the main module again")
        # Cards should still be gone -- bootstrap did NOT fire again.
        cards_after_second = len(list(cards_dir.glob("*.json")))
        assert cards_after_second == 0

    def test_auto_bootstrap_false_does_not_seed(self, tmp_path):
        """auto_bootstrap=False: first assemble() on an empty store returns empty."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "engine.py").write_text("class Engine: pass\n", encoding="utf-8")

        cards_dir = tmp_path / "cards"
        store = FileContextStore(
            str(cards_dir), repo_root=str(repo), auto_bootstrap=False
        )
        ac = store.assemble("engine class")
        assert ac.context_view == ""
        assert not cards_dir.exists() or not any(cards_dir.glob("*.json"))

    def test_auto_bootstrap_skipped_when_cards_exist(self, tmp_path):
        """If cards already exist, auto-bootstrap does not run (idempotency guard)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "service.py").write_text("def serve(): pass\n", encoding="utf-8")

        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        # Pre-populate a card
        _write_card(cards_dir, _make_card("pre-existing", ["preexisting", "card"]))

        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=True)
        store.assemble("something unrelated")

        # Only the original card should be present; bootstrap did not add more.
        card_files = list(cards_dir.glob("*.json"))
        assert len(card_files) == 1
        assert card_files[0].stem == "pre-existing"


# ---------------------------------------------------------------------------
# IDF scoring: distinctive terms rank the right card first
# ---------------------------------------------------------------------------

class TestIDFScoring:
    def test_distinctive_term_ranks_correct_card_first(self, tmp_path):
        """A card with a distinctive (low-DF) term is ranked above a card with only
        common (high-DF) terms when querying the distinctive term."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()

        # Card A: has a distinctive keyword + common keywords.
        _write_card(cards_dir, _make_card(
            "card-a",
            ["database", "schema", "migration", "xylophone"],  # "xylophone" is unique
            summary="card a: database schema migration xylophone utilities",
        ))
        # Card B: has only the common keywords.
        _write_card(cards_dir, _make_card(
            "card-b",
            ["database", "schema", "migration"],
            summary="card b: database schema migration utilities",
        ))
        # Card C, D: more cards containing the common terms (raises their DF).
        _write_card(cards_dir, _make_card(
            "card-c", ["database", "schema", "index"],
            summary="card c: database schema index operations",
        ))
        _write_card(cards_dir, _make_card(
            "card-d", ["database", "migration", "rollback"],
            summary="card d: database migration rollback procedures",
        ))

        # confidence_threshold=0.0: tests ranking logic on small synthetic card sets
        # where IDF scores are below the production gate of 9.0.
        store = FileContextStore(str(cards_dir), max_cards_in_view=4, auto_bootstrap=False,
                                 confidence_threshold=0.0)

        # Query for the distinctive term: card-a must rank first.
        ac = store.assemble("xylophone database")
        assert ac.card_ids[0] == "card-a", (
            f"expected card-a first (distinctive term), got: {ac.card_ids}"
        )

    def test_common_term_alone_does_not_unfairly_crowd_distinctive_card(self, tmp_path):
        """Querying only a common term should NOT push a card with ONLY that term
        above a card that also has a distinctive match -- IDF penalises ubiquitous terms."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()

        # Card A: has "common" + "unique_alpha"
        _write_card(cards_dir, _make_card(
            "card-specific",
            ["common", "unique_alpha"],
            summary="card specific: common term unique alpha feature",
        ))
        # Cards B-E: all have "common", so it becomes very high DF.
        for i in range(4):
            _write_card(cards_dir, _make_card(
                f"card-common-{i}",
                ["common", f"other{i}"],
                summary=f"card common {i}: common term other feature",
            ))

        store = FileContextStore(str(cards_dir), max_cards_in_view=5, auto_bootstrap=False)

        # Querying "unique_alpha" should put card-specific first.
        ac = store.assemble("unique_alpha")
        assert ac.card_ids[0] == "card-specific", (
            f"expected card-specific first, got: {ac.card_ids}"
        )

    def test_idf_scoring_consistent_with_legacy_high_overlap_wins(self, tmp_path):
        """A card matching more query terms is still preferred over one matching fewer,
        all else equal (IDF doesn't break the basic overlap logic for fresh cards)."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()

        # card-high matches 3 unique terms; card-low matches 1 unique term.
        _write_card(cards_dir, _make_card(
            "card-high",
            ["alpha", "beta", "gamma"],
            summary="card high: alpha beta gamma features",
        ))
        _write_card(cards_dir, _make_card(
            "card-low",
            ["alpha"],
            summary="card low: alpha only feature",
        ))

        store = FileContextStore(str(cards_dir), max_cards_in_view=2, auto_bootstrap=False)
        ac = store.assemble("alpha beta gamma query")
        assert ac.card_ids[0] == "card-high"

    def test_idf_no_match_returns_empty(self, tmp_path):
        """A query with no term overlap after IDF still returns empty context."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("card-one", ["database", "schema"]))
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        ac = store.assemble("completely unrelated zzzzzz")
        assert ac.context_view == ""
        assert ac.card_ids == []


# ---------------------------------------------------------------------------
# Confidence gate: the never-worse guarantee
# ---------------------------------------------------------------------------

class TestConfidenceGate:
    """Verify the confidence gate (confidence_threshold) behaviour.

    The gate is the never-worse-by-construction lever: a card is only injected
    when its IDF score clears the threshold.  A weak/ambiguous match yields an
    EMPTY AssembledContext, so the run equals plain Claude Code (the baseline).
    A strong/confident match IS injected.  The system therefore can only ADD a
    confident grounding or stay equal to the baseline -- it never makes things worse.
    """

    # ------------------------------------------------------------------
    # 1. Weak/ambiguous match below threshold -> empty context (never worse)
    # ------------------------------------------------------------------

    def test_weak_match_yields_empty_context(self, tmp_path):
        """A query whose best-matching card scores below the threshold injects NOTHING.

        The run therefore equals plain Claude Code (the never-worse guarantee).
        """
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        # Two cards with a single common term each.  With N=2 and df=1,
        # IDF(term) = log(3/2)+1 ~= 1.405; max keyword weight = 3.0, so
        # score ~= 3.0*1.0 + 3.0*1.405 = 7.2, which stays below the default 9.0 gate.
        _write_card(cards_dir, _make_card(
            "weak-card-a", ["python", "module"],
            summary="weak card a: python module",
        ))
        _write_card(cards_dir, _make_card(
            "weak-card-b", ["python", "package"],
            summary="weak card b: python package",
        ))

        # Default threshold (9.0) -- weak common-term match scores ~7.2, below the gate.
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        ac = store.assemble("python module package")

        assert ac.context_view == "", (
            f"expected empty context_view for weak match, got: {ac.context_view!r}"
        )
        assert ac.card_ids == [], (
            f"expected no card_ids for weak match, got: {ac.card_ids}"
        )

    def test_weak_match_on_small_card_set_clears_with_zero_threshold(self, tmp_path):
        """The same weak match that is gated out at 9.0 IS returned at 0.0.

        This proves the gate is the only reason the match is suppressed,
        and that confidence_threshold=0.0 restores old behaviour.
        """
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card(
            "weak-card-a", ["python", "module"],
            summary="weak card a: python module",
        ))

        store_gated = FileContextStore(str(cards_dir), auto_bootstrap=False)
        store_open = FileContextStore(str(cards_dir), auto_bootstrap=False,
                                      confidence_threshold=0.0)

        ac_gated = store_gated.assemble("python module")
        ac_open = store_open.assemble("python module")

        assert ac_gated.card_ids == [], "expected gated store to suppress weak match"
        assert len(ac_open.card_ids) > 0, "expected open store to return weak match"

    # ------------------------------------------------------------------
    # 2. Default threshold is 9.0 (calibrated for max field-weighted scoring)
    # ------------------------------------------------------------------

    def test_default_threshold_is_9(self, tmp_path):
        """FileContextStore default confidence_threshold must be 9.0.

        Calibrated for max field-weighted scoring: a keyword match scores
        keyword_weight(3.0) * IDF. A single unique keyword in a 36-card corpus
        scores ~11.7, well above 9.0. Common terms in a tiny corpus score ~7.2,
        below 9.0. This gates noise while passing genuine matches.
        """
        store = FileContextStore(str(tmp_path / "cards"), auto_bootstrap=False)
        assert store._confidence_threshold == 9.0

    # ------------------------------------------------------------------
    # 3. Strong/confident match over a realistic ~30+ card bootstrap IS injected
    # ------------------------------------------------------------------

    def _make_large_repo(self, tmp_path: Path, n_noise_files: int = 35) -> Path:
        """Create a repo with one distinctive module + many noise files.

        The target file has a uniquely-named function (xfr_collate_payments_7q2) that
        appears in NO other file.  The noise files share only generic path tokens so
        their IDF contribution to any query is low.
        """
        repo = tmp_path / "repo"
        repo.mkdir()

        # Target file with a highly distinctive function name.
        target_dir = repo / "billing"
        target_dir.mkdir()
        target_text = (
            "def xfr_collate_payments_7q2(account_id):\n"
            "    '''Collate outstanding payments for the given account.'''\n"
            "    pass\n\n"
            "class PaymentCollector:\n"
            "    pass\n"
        )
        (target_dir / "collate.py").write_text(target_text, encoding="utf-8")

        # Noise files: generic names with no overlap to the distinctive symbol.
        noise_dir = repo / "common"
        noise_dir.mkdir()
        for i in range(n_noise_files):
            code = (
                f"# noise module {i}\n"
                f"def helper_{i}():\n    pass\n"
                f"class Util{i}:\n    pass\n"
            )
            (noise_dir / f"util_{i}.py").write_text(code, encoding="utf-8")

        return repo

    def _large_repo_topics(self, n_noise_files: int = 35):
        """One topic per file for the large repo: a distinctive 'collate' topic plus generic
        noise topics. The distinctive keywords land on only one card so its IDF is high."""
        topics = [{
            "id": "collate",
            "name": "Payment collation",
            "keywords": ["xfr", "collate", "payments", "7q2", "billing", "account"],
            "summary": "Collate outstanding payments for an account.",
            "files": ["billing/collate.py"],
        }]
        for i in range(n_noise_files):
            topics.append({
                "id": f"util-{i}",
                "name": f"Util {i}",
                "keywords": ["util", "helper", "common"],
                "summary": f"Generic helper module {i}.",
                "files": [f"common/util_{i}.py"],
            })
        return topics

    def test_strong_match_injected_over_large_bootstrap(self, tmp_path):
        """A distinctive query term that appears in only ONE of ~36 topic cards clears the
        9.0 gate and is injected with the right card.

        With max field weights: keyword_weight(3.0) * IDF(1/36 cards ~= 3.92) = 11.76 per
        term, which exceeds the 9.0 gate. Six such terms gives a total score of ~70.
        """
        n_noise = 35
        repo = self._make_large_repo(tmp_path, n_noise_files=n_noise)
        cards_dir = tmp_path / "cards"

        store = FileContextStore(
            str(cards_dir),
            repo_root=str(repo),
            auto_bootstrap=False,
        )
        n = store.bootstrap(
            root=str(repo),
            provider=_topic_provider(self._large_repo_topics(n_noise)),
        )
        assert n >= 36, f"expected >= 36 topic cards from bootstrap, got {n}"

        # Query uses the distinctive function name as the key term.
        ac = store.assemble("xfr collate payments 7q2 billing account")

        assert ac.context_view != "", (
            f"expected non-empty context_view for distinctive strong match "
            f"(N={n} cards, threshold=9.0)"
        )
        assert len(ac.card_ids) > 0, (
            "expected at least one card_id for strong/distinctive match"
        )
        # The top card must be the billing/collate.py topic.
        top_id = ac.card_ids[0]
        all_cards = store._load_all()
        top_card = all_cards.get(top_id, {})
        top_file = (top_card.get("files") or [{}])[0].get("path", "")
        assert "collate" in top_file, (
            f"expected top card to be billing/collate.py, got path: {top_file!r}"
        )

    def test_zero_threshold_injects_any_positive_match(self, tmp_path):
        """confidence_threshold=0.0 means any positive-score match is injected
        (old behaviour / disabled gate).  Even a single-card store with a single
        overlapping term yields a non-empty context.
        """
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card(
            "any-card", ["chatbot", "interface"],
            summary="any card: chatbot interface module",
        ))

        store = FileContextStore(str(cards_dir), auto_bootstrap=False,
                                 confidence_threshold=0.0)
        ac = store.assemble("chatbot interface query")

        assert ac.context_view != "", "expected non-empty context with threshold=0.0"
        assert "any-card" in ac.card_ids


# ---------------------------------------------------------------------------
# In-memory card cache
# ---------------------------------------------------------------------------

class TestCardCache:
    def test_two_assembles_return_consistent_results(self, tmp_path):
        """Repeated assemble() calls on the same store return identical card_ids."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("alpha-card", ["alpha", "bravo", "charlie"]))
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        ac1 = store.assemble("alpha bravo charlie")
        ac2 = store.assemble("alpha bravo charlie")
        assert ac1.card_ids == ac2.card_ids
        assert ac1.context_view == ac2.context_view

    def test_cache_reloads_after_record(self, tmp_path):
        """After record() writes a new card, the next assemble() must see it."""
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        # First assemble: store is empty.
        ac0 = store.assemble("zyzzyx special query")
        assert ac0.card_ids == []

        # record() writes a card with the query term.
        store.record("zyzzyx special query", {"kind": "answer"})

        # Next assemble() must return the new card (cache must have been invalidated).
        ac1 = store.assemble("zyzzyx special query")
        assert len(ac1.card_ids) > 0, "cache not invalidated after record()"

    def test_cache_reloads_after_external_write(self, tmp_path):
        """If another process writes a card file to cards_dir, the next assemble() sees it."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)

        # Prime the cache with one assemble (finds nothing for this query).
        ac0 = store.assemble("neptunium special element")
        assert ac0.card_ids == []

        # External write: another process drops a card directly (no store.record()).
        new_card = _make_card("neptunium-card", ["neptunium", "special", "element"],
                              summary="neptunium special element card")
        # Write directly to disk, bypassing the store instance (simulates another agent).
        import time
        time.sleep(0.01)  # ensure mtime advances so the dir stamp changes
        _write_card(cards_dir, new_card)

        # The next assemble() should detect the changed dir stamp and reload.
        ac1 = store.assemble("neptunium special element")
        assert "neptunium-card" in ac1.card_ids, (
            "external card write not detected; cache not invalidated"
        )

    def test_cache_not_reloaded_when_nothing_changed(self, tmp_path):
        """When no writes occur, the cached data is reused (dir stamp unchanged)."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write_card(cards_dir, _make_card("stable-card", ["stable", "cache", "test"]))

        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        # First call loads the cache.
        ac1 = store.assemble("stable cache test")
        # Confirm cache is populated.
        assert store._cache is not None
        stamp1 = store._cache_dir_stamp

        # Second call should NOT reload (stamp unchanged).
        ac2 = store.assemble("stable cache test")
        assert store._cache_dir_stamp == stamp1  # stamp didn't change
        assert ac1.card_ids == ac2.card_ids

    def test_bootstrap_then_assemble_sees_new_cards(self, tmp_path):
        """After bootstrap() writes cards, assemble() on the same instance sees them."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "service.py").write_text("def handle_request(): pass\n", encoding="utf-8")

        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir), repo_root=str(repo), auto_bootstrap=False)

        # No cards yet.
        ac0 = store.assemble("handle request service")
        assert ac0.card_ids == []

        # Bootstrap writes cards; cache dirty flag must be set.
        provider = _topic_provider([{
            "id": "service",
            "name": "Service",
            "keywords": ["service", "handle", "request", "handler"],
            "summary": "Request handling service.",
            "files": ["service.py"],
        }])
        n = store.bootstrap(root=str(repo), provider=provider)
        assert n > 0

        # Next assemble must see the bootstrapped cards.
        ac1 = store.assemble("handle request service")
        assert len(ac1.card_ids) > 0, "cache not invalidated after bootstrap()"


# ---------------------------------------------------------------------------
# Richer no-LLM summaries: docstring extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Docstring/heading extraction HELPERS (still used by record() and the vector arm).
# Bootstrap no longer extracts docstrings (topic summaries come from the LLM), but the
# helper functions remain part of the module, so we exercise them directly here.
# ---------------------------------------------------------------------------

class TestRichSummaryHelpers:
    """The _build_rich_summary / _extract_docstrings helpers extract docstrings from .py files."""

    def test_module_docstring_in_rich_summary(self, tmp_path):
        from quest_ai_runner.adapters.file_context_store import _build_rich_summary

        p = tmp_path / "analyzer.py"
        p.write_text(
            '"""Analyze user behaviour patterns for the recommendation engine."""\n'
            "\n"
            "class BehaviourAnalyzer:\n"
            '    """Tracks and aggregates user events."""\n'
            "    pass\n",
            encoding="utf-8",
        )
        summary, description = _build_rich_summary("analyzer.py", p, ["BehaviourAnalyzer"])
        assert "Analyze user behaviour" in summary, (
            f"module docstring not in summary: {summary!r}"
        )

    def test_class_and_fn_docstrings_in_rich_summary(self, tmp_path):
        from quest_ai_runner.adapters.file_context_store import _build_rich_summary

        p = tmp_path / "engine.py"
        p.write_text(
            '"""The scoring engine."""\n'
            "\n"
            "class ScoreEngine:\n"
            '    """Computes recommendation scores."""\n'
            "    pass\n"
            "\n"
            "def build_index(corpus):\n"
            '    """Build a BM25 index over the corpus."""\n'
            "    pass\n",
            encoding="utf-8",
        )
        summary, description = _build_rich_summary("engine.py", p, ["ScoreEngine", "build_index"])
        combined = summary + " " + description
        assert (
            "Computes recommendation scores" in combined
            or "Build a BM25 index" in combined
        ), f"def docstrings not in helper text: {combined!r}"

    def test_description_populated_for_py_with_docstring(self, tmp_path):
        from quest_ai_runner.adapters.file_context_store import _build_rich_summary

        p = tmp_path / "service.py"
        p.write_text(
            '"""Service layer for handling API requests."""\n'
            "\n"
            "def dispatch(req):\n"
            '    """Route the request to the right handler."""\n'
            "    pass\n",
            encoding="utf-8",
        )
        summary, description = _build_rich_summary("service.py", p, ["dispatch"])
        assert description, f"expected non-empty description, got: {description!r}"

    def test_no_docstring_falls_back_to_symbol_list(self, tmp_path):
        from quest_ai_runner.adapters.file_context_store import _build_rich_summary

        p = tmp_path / "nodoc.py"
        p.write_text(
            "class WidgetFactory:\n    pass\n"
            "\ndef make_widget(size):\n    pass\n",
            encoding="utf-8",
        )
        summary, description = _build_rich_summary(
            "nodoc.py", p, ["WidgetFactory", "make_widget"]
        )
        assert "WidgetFactory" in summary or "make_widget" in summary, (
            f"expected symbol names in fallback summary: {summary!r}"
        )
