"""Offline tests for the two UX features:

Feature 1 -- INSTANT ACK
  * instant_ack=True: an immediate "Looking into this..." status is emitted,
    then a cheap ack call is made and its text is emitted as EVENT_PARTIAL.
  * The ack prompt restates the user's request.
  * A provider failure does NOT break the run (it completes normally).
  * instant_ack=False (default): no ack call, no extra status.

Feature 2 -- CONTEXT TRANSPARENCY
  * FileContextStore.assemble populates sources with adapter="keyword".
  * HybridContextAssembler merges sources from both arms.
  * Orchestrator emits a STATUS event whose text names the adapters/files
    when the assembler returns sources.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core.adapters import (
    AssembledContext,
    EVENT_PARTIAL,
    EVENT_STATUS,
    ContextAssembler,
    ProgressEvent,
    StreamSink,
    Mode,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler

from .conftest import StubProvider, StubRetrieval


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_orch(provider, retrieval, cfg=None, assembler=None):
    return Orchestrator(
        retrieval=retrieval,
        provider=provider,
        registry=ModelRegistry(provider),
        config=cfg,
        context_assembler=assembler,
    )


def _collect_events(orch: Orchestrator, user_message: str,
                    cfg: OrchestratorConfig, assembler=None) -> List[Dict[str, Any]]:
    """Run the orchestrator with a StreamSink and return all emitted event dicts."""
    events: List[Dict[str, Any]] = []
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
    retrieval = StubRetrieval()
    o = _make_orch(provider, retrieval, cfg=cfg, assembler=assembler)
    sink = StreamSink(lambda ev: events.append(ev))
    o.run(user_message, mode=Mode.LIVE, sink=sink)
    return events


# ---------------------------------------------------------------------------
# Feature 1 — INSTANT ACK
# ---------------------------------------------------------------------------

class TestInstantAck:
    """Tests for OrchestratorConfig.instant_ack."""

    def _provider_with_ack(self, ack_text: str) -> StubProvider:
        """Provider whose first answer() call returns ack_text; subsequent calls return generic."""
        answers = [ack_text]

        class _AckProvider(StubProvider):
            def __init__(self_inner):
                super().__init__(decisions=[{"action": "answer", "rationale": "done"}])
                self_inner._answers = list(answers)
                self_inner.ack_prompts: List[str] = []

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                # Capture the content of the first message to check the ack prompt.
                content = (messages[0].get("content", "") if messages else "")
                self_inner.ack_prompts.append(content)
                if self_inner._answers:
                    return self_inner._answers.pop(0)
                return "STUB ANSWER [grounded_on:False]"

        return _AckProvider()

    def test_instant_ack_off_by_default(self):
        """Default config: no ack call, no extra status tick."""
        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval())
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("What is the answer?", mode=Mode.LIVE, sink=sink)

        partial_events = [e for e in events if e["type"] == EVENT_PARTIAL]
        assert not partial_events, "No EVENT_PARTIAL should be emitted when instant_ack=False"
        # The first status should be "planning..." not "Looking into this..."
        first_status = next((e for e in events if e["type"] == EVENT_STATUS), None)
        assert first_status is not None
        assert "looking" not in first_status.get("text", "").lower()

    def test_instant_ack_emits_immediate_status(self):
        """instant_ack=True: 'Looking into this...' is emitted before planning."""
        cfg = OrchestratorConfig(instant_ack=True)
        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("What is the answer?", mode=Mode.LIVE, sink=sink)

        status_texts = [e.get("text", "") for e in events if e["type"] == EVENT_STATUS]
        assert any("looking" in t.lower() for t in status_texts), (
            f"Expected a 'Looking into this...' status; got: {status_texts}"
        )
        # It should be the FIRST status event.
        first_status = next(e for e in events if e["type"] == EVENT_STATUS)
        assert "looking" in first_status.get("text", "").lower()

    def test_instant_ack_emits_partial_as_narration(self):
        """The first narration beat is emitted as EVENT_PARTIAL (data={narration:True}) and is
        generated with the user's new message in view (continuity), not as a spinner status tick."""
        cfg = OrchestratorConfig(instant_ack=True)
        ack_text = "Sure, let me look into your pricing question."

        class _TrackingProvider(StubProvider):
            def __init__(self_inner):
                super().__init__(decisions=[{"action": "answer", "rationale": "done"}])
                self_inner._first_answer = True
                self_inner.ack_prompt_captured: Optional[str] = None

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                if self_inner._first_answer:
                    self_inner._first_answer = False
                    # Capture ALL message text (the user's new message lives in the user turn).
                    self_inner.ack_prompt_captured = " ".join(
                        m.get("content", "") for m in messages
                    )
                    return ack_text
                return "STUB ANSWER [grounded_on:False]"

        provider = _TrackingProvider()
        events: List[Dict[str, Any]] = []
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("What is the pricing?", mode=Mode.LIVE, sink=sink)

        # The first beat is emitted as EVENT_PARTIAL with data={"narration": True} so consumers
        # render it as a live (non-persisted) assistant line / spoken on voice, not a spinner tick.
        from quest_ai_runner.core.adapters import EVENT_PARTIAL
        narration_events = [e for e in events if e["type"] == EVENT_PARTIAL
                            and isinstance(e.get("data"), dict) and e["data"].get("narration")]
        assert narration_events, (
            f"Expected EVENT_PARTIAL with data.narration=True for {ack_text!r}; "
            f"got partial events: {[e for e in events if e['type'] == EVENT_PARTIAL]}"
        )
        # Emitted with a trailing space so consecutive beats read as separate sentences when a
        # consumer concatenates them into one bubble; the line itself is unchanged.
        assert narration_events[0].get("text").rstrip() == ack_text

        # The narration is generated with the user's new message in view (continuity).
        assert provider.ack_prompt_captured is not None
        assert "pricing" in provider.ack_prompt_captured.lower()

    def test_narrate_flag_enables_narration_without_instant_ack(self):
        """narrate=True turns on the conversational train of thought even when instant_ack is False."""
        cfg = OrchestratorConfig(narrate=True)
        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}],
                                answer_text="STUB ANSWER")
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("What is the answer?", mode=Mode.LIVE, sink=sink)

        narration = [e for e in events if e["type"] == EVENT_PARTIAL
                     and isinstance(e.get("data"), dict) and e["data"].get("narration")]
        assert narration, "narrate=True should emit at least one narration partial"

    def test_per_step_beat_relays_planner_rationale_no_duplicate(self):
        """Approach B: on a read step, the planner's (conversational) rationale is relayed as the
        narration beat, and the plan/replan event carries NO duplicate text (so a consumer won't
        show the line twice). Zero extra LLM call: the beat IS the planner's rationale."""
        from quest_ai_runner.core.adapters import EVENT_PLAN, EVENT_REPLAN
        beat = "I'm pulling up your quest history so we can see where things break down."
        cfg = OrchestratorConfig(narrate=True)
        # read -> answer. The read step's rationale is the conversational beat.
        decisions = [
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": beat,
             "model_tier": "sonnet"},
            {"action": "answer", "rationale": "answering now", "model_tier": "sonnet"},
        ]

        class _AckThenAnswer(StubProvider):
            """First answer() call is the instant ack; later calls are the grounded answer."""
            def __init__(self_inner):
                super().__init__(decisions=list(decisions))
                self_inner._first = True

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                if self_inner._first:
                    self_inner._first = False
                    return "On it, let me dig in."
                return "STUB ANSWER [grounded_on:False]"

        provider = _AckThenAnswer()
        retrieval = StubRetrieval(files={"README.md": "some content"})
        events: List[Dict[str, Any]] = []
        orch = _make_orch(provider, retrieval, cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("why do I keep falling off?", mode=Mode.LIVE, sink=sink,
                 rep_preamble="You are Maya, a warm, direct coach.")

        # The planner's conversational rationale was relayed as a narration partial.
        narration = [e.get("text", "").rstrip() for e in events if e["type"] == EVENT_PARTIAL
                     and isinstance(e.get("data"), dict) and e["data"].get("narration")]
        assert beat in narration, f"Expected the planner rationale relayed as a beat; got {narration}"

        # The plan/replan events carry NO text (rationale is spoken as the beat, not duplicated).
        plan_texts = [e.get("text") for e in events if e["type"] in (EVENT_PLAN, EVENT_REPLAN)]
        assert plan_texts, "Expected at least one plan/replan event"
        assert all(t is None for t in plan_texts), (
            f"plan/replan events should carry no duplicate text when narrating; got {plan_texts}"
        )

        # The planner prompt for this run carried the conversational rationale instruction AND the
        # injected persona (Approach B: the planner writes the beat in the rep's voice, no extra call).
        assert any("SPOKEN line" in p for p in provider.plan_prompts), \
            "Planner prompt should switch to the conversational rationale instruction when narrating"
        assert any("Maya" in p for p in provider.plan_prompts), \
            "Planner prompt should carry the rep persona when narrating"

    def test_planner_rationale_plain_when_not_narrating(self):
        """Without narration, the planner keeps its terse rationale instruction (no persona, no
        conversational spoken-line directive) so planning behavior is unchanged."""
        cfg = OrchestratorConfig(narrate=False, instant_ack=False)
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval())
        orch.run("plain question", mode=Mode.LIVE, sink=StreamSink(lambda _: None),
                 rep_preamble="You are Maya.")
        assert provider.plan_prompts
        assert all("SPOKEN line" not in p for p in provider.plan_prompts)
        assert any("one sentence" in p for p in provider.plan_prompts)

    def test_instant_ack_prompt_forbids_em_dashes(self):
        """The ack prompt explicitly instructs the model not to use em dashes."""
        cfg = OrchestratorConfig(instant_ack=True)
        captured_prompts: List[str] = []

        class _CapturingProvider(StubProvider):
            def __init__(self_inner):
                super().__init__(decisions=[{"action": "answer", "rationale": "done"}])
                self_inner._first_answer = True

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                content = messages[0].get("content", "") if messages else ""
                captured_prompts.append(content)
                if self_inner._first_answer:
                    self_inner._first_answer = False
                    return "I am looking into your request."
                return "STUB ANSWER [grounded_on:False]"

        provider = _CapturingProvider()
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda _: None)
        orch.run("some request", mode=Mode.LIVE, sink=sink)

        # At least one prompt should be the ack prompt.
        assert captured_prompts, "No answer calls were made"
        ack_prompt = captured_prompts[0]
        assert "em dash" in ack_prompt.lower() or "em dashes" in ack_prompt.lower(), (
            f"Ack prompt does not forbid em dashes: {ack_prompt[:200]}"
        )

    def test_instant_ack_provider_failure_does_not_break_run(self):
        """If the ack provider call raises, the run still completes normally."""
        cfg = OrchestratorConfig(instant_ack=True)

        class _FailingProvider(StubProvider):
            def __init__(self_inner):
                super().__init__(decisions=[{"action": "answer", "rationale": "done"}])
                self_inner._first_answer = True

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                if self_inner._first_answer:
                    self_inner._first_answer = False
                    raise RuntimeError("provider is broken")
                return "STUB ANSWER [grounded_on:False]"

        provider = _FailingProvider()
        events: List[Dict[str, Any]] = []
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        # Should NOT raise.
        result = orch.run("What is the answer?", mode=Mode.LIVE, sink=sink)

        # Run completes normally.
        assert result.kind == "answer"
        # No partial events (ack is now EVENT_STATUS, not PARTIAL; and this ack failed anyway).
        partial_events = [e for e in events if e["type"] == EVENT_PARTIAL]
        assert not partial_events

    def test_instant_ack_run_completes_normally(self):
        """With instant_ack=True the run still produces a correct answer result."""
        cfg = OrchestratorConfig(instant_ack=True)
        events: List[Dict[str, Any]] = []
        provider = StubProvider(
            decisions=[{"action": "answer", "rationale": "done"}],
            answer_text="STUB ANSWER",
        )

        class _OneAckProvider(StubProvider):
            def __init__(self_inner):
                super().__init__(decisions=[{"action": "answer", "rationale": "done"}])
                self_inner._first = True

            def answer(self_inner, messages, *, model, system=None) -> str:
                self_inner.answer_calls += 1
                self_inner.last_answer_messages = messages
                if self_inner._first:
                    self_inner._first = False
                    return "I am looking into this now."
                return "STUB ANSWER [grounded_on:False]"

        provider = _OneAckProvider()
        orch = _make_orch(provider, StubRetrieval(), cfg=cfg)
        sink = StreamSink(lambda ev: events.append(ev))
        result = orch.run("What is the answer?", mode=Mode.LIVE, sink=sink)

        assert result.kind == "answer"
        # The run emitted a DONE event.
        assert any(e["type"] == "done" for e in events)


# ---------------------------------------------------------------------------
# Feature 2 — CONTEXT TRANSPARENCY
# ---------------------------------------------------------------------------

class TestContextTransparency:
    """Tests for AssembledContext.sources and Orchestrator source-summary STATUS event."""

    # --- AssembledContext.sources field (backward compat) ---

    def test_assembled_context_sources_defaults_to_empty(self):
        """Backward compat: AssembledContext() has sources=[] by default."""
        ac = AssembledContext()
        assert ac.sources == []

    def test_assembled_context_sources_can_be_set(self):
        ac = AssembledContext(
            sources=[{"adapter": "keyword", "label": "docstring cards", "items": ["a.py"]}]
        )
        assert len(ac.sources) == 1
        assert ac.sources[0]["adapter"] == "keyword"

    # --- FileContextStore populates sources ---

    def _make_cards_dir_with_cards(self, tmp_path: Path) -> Path:
        """Create a temp cards dir with one card pinning two files."""
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        card = {
            "id": "test-card",
            "keywords": ["orchestrator", "run", "plan"],
            "summary": "orchestrator.py -- the brain",
            "files": [
                {"path": "core/orchestrator.py", "sha256": "", "mtime": 0.0,
                 "git_sha": "", "why": "main", "symbols": ["run"]},
                {"path": "core/adapters.py", "sha256": "", "mtime": 0.0,
                 "git_sha": "", "why": "interfaces", "symbols": ["ProgressEvent"]},
            ],
            "conventions": [],
            "provenance": {"created_by_task": "", "model": "", "created_at": "",
                           "last_verified_at": ""},
            "usage_count": 1,
            "last_outcome": "met",
        }
        (cards_dir / "test-card.json").write_text(
            json.dumps(card), encoding="utf-8"
        )
        return cards_dir

    def test_file_context_store_sources_keyword(self, tmp_path):
        """FileContextStore.assemble populates sources with adapter='keyword'."""
        cards_dir = self._make_cards_dir_with_cards(tmp_path)
        store = FileContextStore(
            str(cards_dir),
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )
        result = store.assemble("orchestrator run plan")

        assert result.sources, "FileContextStore should populate sources"
        assert len(result.sources) == 1
        src = result.sources[0]
        assert src["adapter"] == "keyword"
        assert src["label"] == "docstring cards"
        # The items should include the pinned file paths.
        assert "core/orchestrator.py" in src["items"]
        assert "core/adapters.py" in src["items"]

    def test_file_context_store_no_match_empty_sources(self, tmp_path):
        """If no cards match, sources is empty."""
        cards_dir = self._make_cards_dir_with_cards(tmp_path)
        store = FileContextStore(
            str(cards_dir),
            auto_bootstrap=False,
            confidence_threshold=100.0,  # unreachable threshold
        )
        result = store.assemble("something completely unrelated xyz")
        assert result.sources == []

    # --- HybridContextAssembler merges sources ---

    def test_hybrid_merges_sources_from_both_arms(self):
        """HybridContextAssembler concatenates sources from keyword + vector arms."""

        class _KwAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(
                    context_view="kw context",
                    sources=[{"adapter": "keyword", "label": "docstring cards",
                               "items": ["a.py"]}],
                )
            def record(self, task_text, outcome): pass

        class _VecAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(
                    context_view="vec context",
                    sources=[{"adapter": "vector", "label": "semantic match",
                               "items": ["b.py"]}],
                )
            def record(self, task_text, outcome): pass

        hybrid = HybridContextAssembler(_KwAssembler(), _VecAssembler())
        result = hybrid.assemble("some task")

        assert result.sources, "Hybrid should merge sources"
        adapter_types = {s["adapter"] for s in result.sources}
        assert "keyword" in adapter_types
        assert "vector" in adapter_types
        # keyword arm comes first.
        assert result.sources[0]["adapter"] == "keyword"

    def test_hybrid_merges_sources_when_one_arm_empty(self):
        """If one arm returns no sources, the other arm's sources are preserved."""

        class _KwAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(
                    context_view="kw context",
                    sources=[{"adapter": "keyword", "label": "docstring cards",
                               "items": ["a.py"]}],
                )
            def record(self, task_text, outcome): pass

        class _EmptyVecAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext()
            def record(self, task_text, outcome): pass

        hybrid = HybridContextAssembler(_KwAssembler(), _EmptyVecAssembler())
        result = hybrid.assemble("some task")

        # context_view from kw only (vec empty -> no view).
        assert result.context_view  # kw view should be present
        assert len(result.sources) == 1
        assert result.sources[0]["adapter"] == "keyword"

    # --- Orchestrator emits a STATUS event summarising sources ---

    def test_orchestrator_emits_source_summary_status(self):
        """When assembler returns sources, Orchestrator emits a STATUS with adapter/file names."""
        src_entry = {
            "adapter": "keyword",
            "label": "docstring cards",
            "items": ["core/orchestrator.py", "core/adapters.py"],
        }

        class _SourceAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(
                    context_view="some context",
                    sources=[src_entry],
                )
            def record(self, task_text, outcome): pass

        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval(), assembler=_SourceAssembler())
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("test question", mode=Mode.LIVE, sink=sink)

        status_texts = [e.get("text", "") for e in events if e["type"] == EVENT_STATUS]
        # Find the source-summary status.
        source_status = next(
            (t for t in status_texts if "context from" in t.lower()), None
        )
        assert source_status is not None, (
            f"Expected a 'Context from:' STATUS event; got: {status_texts}"
        )
        # It should name the adapter label and at least one file.
        assert "docstring cards" in source_status
        # File basenames (last segment) should appear.
        assert "orchestrator.py" in source_status or "adapters.py" in source_status

    def test_orchestrator_no_source_summary_when_sources_empty(self):
        """When assembler returns empty sources, no 'Context from:' status is emitted."""

        class _NoSourceAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(context_view="some context", sources=[])
            def record(self, task_text, outcome): pass

        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval(), assembler=_NoSourceAssembler())
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("test question", mode=Mode.LIVE, sink=sink)

        source_status = next(
            (e.get("text", "") for e in events
             if e["type"] == EVENT_STATUS and "context from" in e.get("text", "").lower()),
            None,
        )
        assert source_status is None

    def test_orchestrator_no_source_summary_when_no_assembler(self):
        """Without a ContextAssembler, no 'Context from:' status is ever emitted."""
        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval())
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("test question", mode=Mode.LIVE, sink=sink)

        source_status = next(
            (e for e in events
             if e["type"] == EVENT_STATUS and "context from" in e.get("text", "").lower()),
            None,
        )
        assert source_status is None

    def test_orchestrator_source_summary_names_multiple_adapters(self):
        """With multiple source entries, all adapter labels appear in the status text."""

        class _MultiSourceAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                return AssembledContext(
                    context_view="hybrid context",
                    sources=[
                        {"adapter": "keyword", "label": "docstring cards",
                         "items": ["core/orchestrator.py"]},
                        {"adapter": "vector", "label": "semantic match",
                         "items": ["core/adapters.py"]},
                    ],
                )
            def record(self, task_text, outcome): pass

        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval(), assembler=_MultiSourceAssembler())
        sink = StreamSink(lambda ev: events.append(ev))
        orch.run("test question", mode=Mode.LIVE, sink=sink)

        source_status = next(
            (e.get("text", "") for e in events
             if e["type"] == EVENT_STATUS and "context from" in e.get("text", "").lower()),
            None,
        )
        assert source_status is not None
        assert "docstring cards" in source_status
        assert "semantic match" in source_status

    def test_assembler_failure_does_not_break_run(self):
        """If the assembler raises inside assemble(), the run proceeds unaffected."""

        class _BrokenAssembler:
            def assemble(self, task_text, *, meta=None) -> AssembledContext:
                raise RuntimeError("assembler is broken")
            def record(self, task_text, outcome): pass

        events: List[Dict[str, Any]] = []
        provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
        orch = _make_orch(provider, StubRetrieval(), assembler=_BrokenAssembler())
        sink = StreamSink(lambda ev: events.append(ev))
        result = orch.run("test question", mode=Mode.LIVE, sink=sink)

        assert result.kind == "answer"
        assert any(e["type"] == "done" for e in events)
