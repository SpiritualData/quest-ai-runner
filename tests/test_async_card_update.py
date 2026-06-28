"""Async post-deep context-card updater (prepare for the FUTURE after a deep run).

Covers, all offline with stubs:
  * the FUTURE-CONTEXT section parser (extracts the delimited block; absent -> "");
  * a scripted provider returns a structured edit plan -> the updater calls the card-update API with
    those edits, including a CORRECTION (replace) and a name/description (fields) update;
  * best-effort: a failing updater never raises and never changes the OrchestratorResult; the loop
    invokes it off the result path in a thread;
  * disabled toggle / no card-update store / no provider -> no LLM call, no FUTURE-CONTEXT block in
    the deep brief, deep loop unchanged.

The capability detection is exercised generically: the fake store exposes ``update_card`` +
``add_content`` (it is NOT a FileContextStore), and a composite wrapper is unwrapped to find it.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext, DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DEEP_FUTURE_CONTEXT_INSTRUCTION,
    FUTURE_CONTEXT_DELIMITER,
    Orchestrator,
    OrchestratorConfig,
    _card_update_store,
    _normalize_card_edits,
    _parse_future_context,
    _strip_future_context,
)

from .conftest import StubRetrieval


# --------------------------------------------------------------------------- stubs


class RecordingCardStore:
    """A card-update-capable store (duck-typed: exposes update_card + add_content). Records every
    update_card call. Also a ContextAssembler (assemble/record) so it can be wired as the assembler
    and return current cards for the updater to correct."""

    def __init__(self, current_cards: Optional[List[Dict[str, Any]]] = None):
        self._current = current_cards or []
        self.update_calls: List[Dict[str, Any]] = []

    # ContextAssembler surface
    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext(context_view="", card_metadata=list(self._current))

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None

    # Card-update API surface (the capability the detector looks for)
    def add_content(self, card_id: str, item: Dict[str, Any]) -> bool:
        return True

    def update_card(self, card_id: str, *, add=None, replace=None, remove=None, fields=None) -> bool:
        self.update_calls.append({"card_id": card_id, "add": add, "replace": replace,
                                  "remove": remove, "fields": fields})
        return True


class _Composite:
    """A minimal composite wrapper exposing ``_assemblers`` (the shape _card_update_store unwraps)."""

    def __init__(self, assemblers: List[Any]):
        self._assemblers = assemblers

    def assemble(self, task_text: str, *, meta=None) -> AssembledContext:
        for a in self._assemblers:
            return a.assemble(task_text, meta=meta)
        return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


class CardEditProvider:
    """plan() routes PLANNER decisions (action=deep), VERIFIER verdicts (met=True), and the CARD
    UPDATER (card_edits tool) -> a scripted edit plan. Told apart by the tool schema name."""

    def __init__(self, edits: Dict[str, Any]):
        self._edits = edits
        self.updater_calls = 0
        self.updater_prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        name = tool_schema.get("name")
        if name == "card_edits":
            self.updater_calls += 1
            self.updater_prompts.append(prompt)
            return self._edits
        if name == "goal_verdict":
            return {"met": True}
        # planner
        return {"action": "deep", "goal": "Do the work",
                "deep_subtasks": [{"goal": "Implement the thing", "brief": "implement it"}],
                "rationale": "deep"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class _Runner:
    """A DeepRunner whose output carries a FUTURE-CONTEXT section; records each brief it was given."""

    def __init__(self, output: str):
        self._output = output
        self.briefs: List[str] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None) -> DeepResult:
        self.briefs.append(brief)
        return DeepResult(met=True, output=self._output)


def _orch(provider, runner, *, assembler, **kw):
    return Orchestrator(
        retrieval=StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=runner,
        config=OrchestratorConfig(deep_goal_max_iterations=2,
                                  deep_model_ladder=["sonnet"], **kw.pop("cfg_kw", {})),
        context_assembler=assembler,
        **kw,
    )


_WORKER_OUTPUT = (
    "I implemented the thing and verified it.\n\n"
    f"{FUTURE_CONTEXT_DELIMITER}\n"
    "- collection: Pricing tiers (id: col-123)\n"
    "- key file: backend/pricing.py\n"
)


# --------------------------------------------------------------------------- parser


def test_parser_extracts_future_context_section():
    section = _parse_future_context(_WORKER_OUTPUT)
    assert "Pricing tiers" in section
    assert "col-123" in section
    # The delimiter line itself is sliced off.
    assert FUTURE_CONTEXT_DELIMITER not in section


def test_parser_absent_section_returns_empty():
    assert _parse_future_context("just a normal result, no section") == ""
    assert _parse_future_context("") == ""
    assert _parse_future_context(None) == ""


def test_parser_uses_last_delimiter_occurrence():
    # A worker that echoes the instruction earlier must not confuse the parser: the LAST wins.
    out = (
        f"(my plan references {FUTURE_CONTEXT_DELIMITER} but that was just the instruction)\n"
        "did the work\n\n"
        f"{FUTURE_CONTEXT_DELIMITER}\n- real: keep this (id: x9)\n"
    )
    section = _parse_future_context(out)
    assert "keep this" in section
    assert "x9" in section
    assert "just the instruction" not in section


# --------------------------------------------------------------------------- user-facing stripping


def test_strip_removes_future_context_from_user_output():
    # The FUTURE-CONTEXT section is internal plumbing for the card updater; it must be removed from
    # any deep output shown to the user. The deliverable above the delimiter is kept verbatim.
    shown = _strip_future_context(_WORKER_OUTPUT)
    assert "I implemented the thing and verified it." in shown
    assert FUTURE_CONTEXT_DELIMITER not in shown
    assert "col-123" not in shown                 # the internal bullets are gone
    assert not shown.endswith("\n")               # trailing whitespace trimmed


def test_strip_is_noop_without_delimiter():
    assert _strip_future_context("just a normal result") == "just a normal result"
    assert _strip_future_context("") == ""
    assert _strip_future_context(None) == ""


def test_strip_and_parse_are_complementary():
    # Stripping (user-facing) and parsing (learning) split the same output at the same point:
    # together they cover it with no overlap of the delimiter line.
    shown = _strip_future_context(_WORKER_OUTPUT)
    learned = _parse_future_context(_WORKER_OUTPUT)
    assert FUTURE_CONTEXT_DELIMITER not in shown
    assert FUTURE_CONTEXT_DELIMITER not in learned
    assert "col-123" in learned and "col-123" not in shown


# --------------------------------------------------------------------------- capability detection


def test_card_update_store_detects_direct_and_wrapped():
    store = RecordingCardStore()
    # Direct
    assert _card_update_store(store) is store
    # Wrapped in a composite -> unwrapped
    assert _card_update_store(_Composite([store])) is store
    # An assembler with no card-update API -> None
    class _Plain:
        def assemble(self, t, *, meta=None):
            return AssembledContext()
        def record(self, t, o):
            return None
    assert _card_update_store(_Plain()) is None
    assert _card_update_store(None) is None


# --------------------------------------------------------------------------- updater applies edits


def test_updater_applies_edits_including_correction_and_fields():
    """A structured edit plan is applied via update_card: a content ADD, a CORRECTION (replace), a
    REMOVE, and a name/description (fields) update."""
    edits = {"edits": [{
        "card_id": "pricing",
        "name": "Pricing context",
        "description": "Where the pricing logic and tiers live.",
        "add": [{"type": "collection", "locator": {"name": "Pricing tiers", "id": "col-123"},
                 "why": "used this run"}],
        "replace": [{"item_id": "old-note-1",
                     "item": {"type": "note", "locator": {"text": "tiers are now 3, not 2"}}}],
        "remove": ["stale-7"],
    }]}
    provider = CardEditProvider(edits)
    store = RecordingCardStore(current_cards=[{"id": "pricing", "title": "Pricing", "files": []}])
    orch = _orch(provider, _Runner(_WORKER_OUTPUT), assembler=store)

    n = orch._update_cards_after_deep(
        request="update the pricing docs",
        executed="BRIEF: implement it\n\nRESULT: done",
        future_context=_parse_future_context(_WORKER_OUTPUT),
        ctx_meta={"user_id": "u1"},
    )

    assert n == 1
    assert provider.updater_calls == 1
    assert len(store.update_calls) == 1
    call = store.update_calls[0]
    # User-scoped id (existing slug prefixed for the user).
    assert call["card_id"] == "u:u1:pricing"
    # fields name/description applied.
    assert call["fields"]["name"] == "Pricing context"
    assert "pricing logic" in call["fields"]["description"]
    # content add carries the resolvable collection reference.
    assert call["add"][0]["type"] == "collection"
    assert call["add"][0]["locator"]["id"] == "col-123"
    # correction (replace) carries the (item_id, item) pair.
    assert call["replace"][0][0] == "old-note-1"
    assert call["replace"][0][1]["type"] == "note"
    # removal of a stale item.
    assert call["remove"] == ["stale-7"]


def test_updater_no_edits_on_empty_plan():
    provider = CardEditProvider({"edits": []})
    store = RecordingCardStore()
    orch = _orch(provider, _Runner(_WORKER_OUTPUT), assembler=store)
    n = orch._update_cards_after_deep(request="x", executed="y", future_context="",
                                      ctx_meta={"user_id": "u1"})
    assert n == 0
    assert store.update_calls == []


def test_updater_user_scoping_no_user_id_unchanged_id():
    provider = CardEditProvider({"edits": [{"card_id": "topic",
                                            "add": [{"type": "note",
                                                     "locator": {"text": "fact"}}]}]})
    store = RecordingCardStore()
    orch = _orch(provider, _Runner(_WORKER_OUTPUT), assembler=store)
    n = orch._update_cards_after_deep(request="x", executed="y", future_context="", ctx_meta={})
    assert n == 1
    # No user_id -> id is not prefixed (single-tenant / unscoped behavior).
    assert store.update_calls[0]["card_id"] == "topic"


# --------------------------------------------------------------------------- best-effort / async


def test_failing_updater_never_raises_and_returns_zero():
    class _Boom(RecordingCardStore):
        def update_card(self, *a, **k):
            raise RuntimeError("disk on fire")

    provider = CardEditProvider({"edits": [{"card_id": "c",
                                            "add": [{"type": "note", "locator": {"text": "t"}}]}]})
    store = _Boom()
    orch = _orch(provider, _Runner(_WORKER_OUTPUT), assembler=store)
    # Must not raise; a per-edit failure just yields 0 written.
    n = orch._update_cards_after_deep(request="x", executed="y", future_context="",
                                      ctx_meta={"user_id": "u1"})
    assert n == 0


def test_deep_run_appends_future_context_instruction_and_result_unaffected():
    """When the updater is active, the deep brief carries the FUTURE-CONTEXT instruction, and the
    OrchestratorResult is exactly the deep result (the updater runs off the result path)."""
    provider = CardEditProvider({"edits": [{"card_id": "pricing",
                                            "add": [{"type": "collection",
                                                     "locator": {"name": "Pricing tiers",
                                                                 "id": "col-123"}}]}]})
    store = RecordingCardStore(current_cards=[{"id": "pricing", "title": "Pricing", "files": []}])
    runner = _Runner(_WORKER_OUTPUT)
    orch = _orch(provider, runner, assembler=store)

    res = orch.run("update the pricing docs", context_meta={"user_id": "u1"})

    assert res.kind == "deep"
    assert res.deep_results and res.deep_results[0].met
    # The instruction was appended to the brief the worker saw.
    assert any(DEEP_FUTURE_CONTEXT_INSTRUCTION in b for b in runner.briefs)

    # The background updater runs off the result path; join briefly so the assertion is stable.
    import time
    for _ in range(50):
        if store.update_calls:
            break
        time.sleep(0.02)
    assert provider.updater_calls == 1
    assert store.update_calls and store.update_calls[0]["card_id"] == "u:u1:pricing"


# --------------------------------------------------------------------------- disabled / inert


def test_disabled_toggle_no_call_no_instruction():
    provider = CardEditProvider({"edits": [{"card_id": "x"}]})
    store = RecordingCardStore()
    runner = _Runner("did the work, no section needed")
    orch = _orch(provider, runner, assembler=store, cfg_kw={"async_card_update": False})

    res = orch.run("do the thing", context_meta={"user_id": "u1"})

    assert res.kind == "deep"
    # No FUTURE-CONTEXT instruction in the brief, no updater LLM call.
    assert all(DEEP_FUTURE_CONTEXT_INSTRUCTION not in b for b in runner.briefs)
    assert provider.updater_calls == 0
    assert store.update_calls == []


def test_no_card_update_store_no_call_no_instruction():
    # A plain assembler (no update_card/add_content) -> updater inert, deep unchanged.
    class _Plain:
        def assemble(self, t, *, meta=None):
            return AssembledContext()
        def record(self, t, o):
            return None

    provider = CardEditProvider({"edits": [{"card_id": "x"}]})
    runner = _Runner("did the work")
    orch = _orch(provider, runner, assembler=_Plain())

    res = orch.run("do the thing", context_meta={"user_id": "u1"})

    assert res.kind == "deep"
    assert all(DEEP_FUTURE_CONTEXT_INSTRUCTION not in b for b in runner.briefs)
    assert provider.updater_calls == 0


# --------------------------------------------------------------------------- edit normalization
# Real models (and tool-use via provider.plan) return the edits in several shapes: a
# {"edits": [...]} object, a BARE list of edit objects, or a single {card_id...}. The updater must
# accept all of them (a live smoke test caught the bare-list shape being silently dropped).


def test_normalize_card_edits_accepts_edits_object():
    edit = {"card_id": "c1", "add": [{"type": "note", "locator": {"text": "x"}}]}
    assert _normalize_card_edits({"edits": [edit]}) == [edit]


def test_normalize_card_edits_accepts_bare_list():
    edits = [{"card_id": "c1"}, {"card_id": "c2"}]
    assert _normalize_card_edits(edits) == edits


def test_normalize_card_edits_accepts_single_edit_dict():
    edit = {"card_id": "dreams", "name": "Dreams"}
    assert _normalize_card_edits(edit) == [edit]


def test_normalize_card_edits_unwraps_list_with_wrapper():
    assert _normalize_card_edits([{"edits": [{"card_id": "c1"}]}]) == [{"card_id": "c1"}]


def test_normalize_card_edits_rejects_junk():
    assert _normalize_card_edits(None) == []
    assert _normalize_card_edits("nope") == []
    assert _normalize_card_edits({"nothing": 1}) == []
    assert _normalize_card_edits([1, 2, "x"]) == []
