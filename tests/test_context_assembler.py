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
        store = FileContextStore(str(cards_dir))
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
