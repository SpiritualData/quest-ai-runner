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
    DEFAULT_CARD_MERGE_SIMILARITY,
    FUTURE_CONTEXT_DELIMITER,
    Orchestrator,
    OrchestratorConfig,
    _card_update_store,
    _future_context_for_display,
    _normalize_card_edits,
    _parse_future_context,
    _proposed_card_text,
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


# --------------------------------------------------------------------------- display collector


class _R:
    def __init__(self, output):
        self.output = output


def test_future_context_for_display_collects_bullets():
    out = _future_context_for_display([_R(_WORKER_OUTPUT)])
    assert "Pricing tiers" in out and "col-123" in out
    assert "backend/pricing.py" in out
    assert FUTURE_CONTEXT_DELIMITER not in out
    assert "I implemented the thing" not in out          # only the bullets, not the deliverable


def test_future_context_for_display_drops_none_placeholder():
    none_out = f"did the work\n\n{FUTURE_CONTEXT_DELIMITER}\n- (none)\n"
    assert _future_context_for_display([_R(none_out)]) == ""
    assert _future_context_for_display([_R("no section at all")]) == ""
    assert _future_context_for_display([]) == ""


def test_future_context_for_display_merges_multiple_results():
    a = f"a\n\n{FUTURE_CONTEXT_DELIMITER}\n- from A (id: a1)\n"
    b = f"b\n\n{FUTURE_CONTEXT_DELIMITER}\n- from B (id: b2)\n"
    out = _future_context_for_display([_R(a), _R(b)])
    assert "from A" in out and "from B" in out


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


# --------------------------------------------------------------------------- semantic card-merge
# When the updater would CREATE a new card, it first asks the store's OPTIONAL find_similar_card
# capability whether a sufficiently-similar card already exists for THIS user and, if so, redirects
# the edit to UPDATE that card instead of minting a near-duplicate twin. Detected by duck-typing;
# absent capability or a 1.0 threshold => create-as-before; never crosses user scopes.


class SimilarCardStore(RecordingCardStore):
    """A card-update store that ALSO exposes the optional ``find_similar_card`` vector capability."""

    def __init__(self, *, similar_id=None, only_for_user=None, current_cards=None):
        super().__init__(current_cards=current_cards)
        self._similar_id = similar_id
        self._only_for_user = only_for_user
        self.find_calls: List[Dict[str, Any]] = []

    def find_similar_card(self, text, *, user_id=None, min_score):
        self.find_calls.append({"text": text, "user_id": user_id, "min_score": min_score})
        # Enforce user-scope isolation: only surface a match for the expected user's scope.
        if self._only_for_user is not None and user_id != self._only_for_user:
            return None
        return self._similar_id


def _run_updater(provider, store, *, cfg_kw=None, ctx_meta=None):
    orch = _orch(provider, _Runner(_WORKER_OUTPUT), assembler=store, cfg_kw=cfg_kw or {})
    return orch._update_cards_after_deep(
        request="capture this", executed="BRIEF: x\n\nRESULT: y",
        future_context="", ctx_meta=ctx_meta or {"user_id": "u1"},
    )


def test_proposed_card_text_builds_from_name_description_and_notes():
    text = _proposed_card_text({
        "card_id": "x", "name": "Dreams and stress", "description": "How they correlate.",
        "add": [{"type": "note", "locator": {"text": "runs 5k"}, "why": "habit"}],
    })
    assert "Dreams and stress" in text and "How they correlate." in text
    assert "habit" in text and "runs 5k" in text
    assert _proposed_card_text({"card_id": "x"}) == ""  # nothing to match on


def test_merge_redirects_create_to_similar_existing_card():
    """A would-be NEW card is redirected to UPDATE the similar existing card (no twin)."""
    edits = {"edits": [{"card_id": "newslug", "name": "Dreams and stress",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    store = SimilarCardStore(similar_id="u:u1:dreams")  # no current cards -> create candidate
    n = _run_updater(CardEditProvider(edits), store)
    assert n == 1
    # find_similar_card consulted with the user + default threshold; proposed text carries the name.
    assert len(store.find_calls) == 1
    assert store.find_calls[0]["user_id"] == "u1"
    assert store.find_calls[0]["min_score"] == DEFAULT_CARD_MERGE_SIMILARITY
    assert "Dreams and stress" in store.find_calls[0]["text"]
    # The write targets the EXISTING similar card, not a new u:u1:newslug twin; fields/content merged.
    assert len(store.update_calls) == 1
    assert store.update_calls[0]["card_id"] == "u:u1:dreams"
    assert store.update_calls[0]["fields"]["name"] == "Dreams and stress"
    assert store.update_calls[0]["add"][0]["locator"]["text"] == "fact"


def test_no_merge_when_no_similar_card_creates_as_before():
    edits = {"edits": [{"card_id": "newslug",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    store = SimilarCardStore(similar_id=None)
    n = _run_updater(CardEditProvider(edits), store)
    assert n == 1
    assert len(store.find_calls) == 1
    # No match -> the new card is created under the user-scoped slug, exactly as before.
    assert store.update_calls[0]["card_id"] == "u:u1:newslug"


def test_no_find_similar_capability_unchanged():
    """A store WITHOUT find_similar_card (plain RecordingCardStore) behaves exactly as before."""
    edits = {"edits": [{"card_id": "newslug",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    store = RecordingCardStore()
    n = _run_updater(CardEditProvider(edits), store)
    assert n == 1
    assert store.update_calls[0]["card_id"] == "u:u1:newslug"  # created, no error


def test_merge_never_uses_another_users_match():
    """A similar card that belongs to a DIFFERENT user is not used (scope isolation)."""
    edits = {"edits": [{"card_id": "newslug",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    # The store only returns the match for user u1; the run is for u2.
    store = SimilarCardStore(similar_id="u:u1:dreams", only_for_user="u1")
    n = _run_updater(CardEditProvider(edits), store, ctx_meta={"user_id": "u2"})
    assert n == 1
    assert store.find_calls[0]["user_id"] == "u2"
    # No cross-user merge: a fresh u2-scoped card is created instead.
    assert store.update_calls[0]["card_id"] == "u:u2:newslug"


def test_merge_skips_edits_targeting_existing_card():
    """An edit that targets a card the updater was shown (an UPDATE) never calls find_similar_card."""
    edits = {"edits": [{"card_id": "u:u1:dreams",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    store = SimilarCardStore(similar_id="u:u1:OTHER",
                             current_cards=[{"id": "u:u1:dreams", "title": "Dreams"}])
    n = _run_updater(CardEditProvider(edits), store)
    assert n == 1
    assert store.find_calls == []  # existing-id edit left untouched
    assert store.update_calls[0]["card_id"] == "u:u1:dreams"


def test_merge_disabled_at_threshold_one():
    """card_merge_similarity == 1.0 disables the merge (find_similar_card never consulted)."""
    edits = {"edits": [{"card_id": "newslug",
                        "add": [{"type": "note", "locator": {"text": "fact"}}]}]}
    store = SimilarCardStore(similar_id="u:u1:dreams")
    n = _run_updater(CardEditProvider(edits), store, cfg_kw={"card_merge_similarity": 1.0})
    assert n == 1
    assert store.find_calls == []
    assert store.update_calls[0]["card_id"] == "u:u1:newslug"


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


# =========================================================================== #
# FUTURE-CONTEXT CHANNEL: the bullets must never ride inside a STRICT payload  #
# =========================================================================== #
#
# The card updater learns from FUTURE-CONTEXT bullets, so every deep runner must be asked for them,
# including a CODE GENERATOR (which knows the most reusable facts of all: the ids, schema, and files
# it touched). What must change per runner is the CHANNEL, never the capability:
#
#   * a PROSE runner ends its output with the delimited section (unchanged, today's behaviour);
#   * a STRICT-FORMAT runner (generated code / JSON / a patch) declares
#     ``future_context_channel = FUTURE_CONTEXT_VIA_FIELD`` and returns the bullets in
#     ``DeepResult.future_context``, so nothing is ever appended to its payload.
#
# Both land in the SAME place: ``DeepResult.future_context``, normalized at the runner seam, read by
# the async card updater. These tests hold that line from both ends.

import ast

from quest_ai_runner.core.adapters import FUTURE_CONTEXT_VIA_FIELD, FUTURE_CONTEXT_VIA_OUTPUT
from quest_ai_runner.core.orchestrator import (
    DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION,
    _deep_future_context,
    _future_context_channel,
    _normalize_future_context,
)

# What a code-generating deep runner produces: a payload that must parse as Python, and NOTHING else.
_GENERATED_CODE = (
    "def apply_mutation(doc):\n"
    "    doc['status'] = 'active'\n"
    "    return doc\n"
)
_CODE_FUTURE_CONTEXT = (
    "- collection: quest_goals (id: col-77)\n"
    "- schema: goals carry a 'status' field, values active|done\n"
)


class _CodeRunner:
    """A STRICT-FORMAT deep runner: its output is generated Python, so future context must come back
    out of band. Declares the field channel and fills ``DeepResult.future_context``."""

    future_context_channel = FUTURE_CONTEXT_VIA_FIELD

    def __init__(self, code: str = _GENERATED_CODE, future_context: str = _CODE_FUTURE_CONTEXT,
                 contaminate: bool = False):
        self._code = code
        self._future_context = future_context
        # ``contaminate``: simulate a worker that IGNORES the instruction and appends the prose block
        # to the code anyway. The orchestrator must still hand back a clean payload.
        self._contaminate = contaminate
        self.briefs: List[str] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None) -> DeepResult:
        self.briefs.append(brief)
        output = self._code
        if self._contaminate:
            output = f"{output}\n{FUTURE_CONTEXT_DELIMITER}\n- leaked: this must not reach the payload\n"
        return DeepResult(met=True, output=output, future_context=self._future_context)


def _wait_for_updater(store, tries: int = 100) -> None:
    """The card updater runs off the result path in a thread; give it a moment to land."""
    import time
    for _ in range(tries):
        if store.update_calls:
            return
        time.sleep(0.02)


# --------------------------------------------------------------------------- channel declaration


def test_channel_defaults_to_output_for_any_undeclared_runner():
    # Duck-typed runners are the common case: anything that does not declare a channel is prose, and
    # gets exactly today's behaviour.
    assert _future_context_channel(_Runner("x")) == FUTURE_CONTEXT_VIA_OUTPUT
    assert _future_context_channel(None) == FUTURE_CONTEXT_VIA_OUTPUT
    assert _future_context_channel(object()) == FUTURE_CONTEXT_VIA_OUTPUT
    # A runner that declares nonsense is treated as prose rather than breaking the run.
    class _Weird:
        future_context_channel = "sideways"
    assert _future_context_channel(_Weird()) == FUTURE_CONTEXT_VIA_OUTPUT
    # And a strict-format runner is honoured.
    assert _future_context_channel(_CodeRunner()) == FUTURE_CONTEXT_VIA_FIELD


# --------------------------------------------------------------------------- the ask (per channel)


def test_strict_runner_is_asked_for_future_context_out_of_band():
    """A code generator IS asked for future context (never opted out), but through the field channel:
    it gets the out-of-band instruction and never the 'END your output with...' one."""
    provider = CardEditProvider({"edits": [{"card_id": "goals",
                                            "add": [{"type": "note",
                                                     "locator": {"text": "status field"}}]}]})
    store = RecordingCardStore(current_cards=[{"id": "goals", "title": "Goals", "files": []}])
    runner = _CodeRunner()
    orch = _orch(provider, runner, assembler=store)

    orch.run("add a status mutation", context_meta={"user_id": "u1"})

    assert runner.briefs, "the deep runner was never called"
    # Asked, out of band.
    assert all(DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION in b for b in runner.briefs)
    # NEVER told to append a prose block to a strict payload, and never even shown the delimiter.
    assert all(DEEP_FUTURE_CONTEXT_INSTRUCTION not in b for b in runner.briefs)
    assert all(FUTURE_CONTEXT_DELIMITER not in b for b in runner.briefs)


def test_prose_runner_still_gets_todays_instruction_unchanged():
    """The default (prose) channel is byte-for-byte what it was: existing consumers see no change."""
    provider = CardEditProvider({"edits": [{"card_id": "pricing"}]})
    store = RecordingCardStore(current_cards=[{"id": "pricing", "title": "Pricing", "files": []}])
    runner = _Runner(_WORKER_OUTPUT)
    orch = _orch(provider, runner, assembler=store)

    orch.run("update the pricing docs", context_meta={"user_id": "u1"})

    assert runner.briefs
    assert all(DEEP_FUTURE_CONTEXT_INSTRUCTION in b for b in runner.briefs)
    assert all(DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION not in b for b in runner.briefs)


# ------------------------------------------------- the payload + the learning, end to end


def test_code_payload_stays_valid_code_and_its_future_context_reaches_the_card_updater():
    """THE REGRESSION THIS FIXES. A code-generating runner must return VALID CODE as its payload AND
    still feed the card updater. Before: the worker was told to append a prose block to its output,
    which broke the code, so consumers stripped the instruction out of the brief entirely, which
    silently stopped chat mutations (the turns that did real work) from ever teaching the cards."""
    provider = CardEditProvider({"edits": [{"card_id": "goals",
                                            "add": [{"type": "collection",
                                                     "locator": {"name": "quest_goals",
                                                                 "id": "col-77"}}]}]})
    store = RecordingCardStore(current_cards=[{"id": "goals", "title": "Goals", "files": []}])
    runner = _CodeRunner()
    orch = _orch(provider, runner, assembler=store)

    res = orch.run("add a status mutation", context_meta={"user_id": "u1"})

    # 1. THE PAYLOAD: still valid Python, with no prose appended.
    assert res.kind == "deep"
    payload = res.deep_results[0].output
    ast.parse(payload)                                   # raises SyntaxError if contaminated
    assert FUTURE_CONTEXT_DELIMITER not in payload
    assert "col-77" not in payload

    # 2. THE LEARNING: the bullets came back out of band and are the result's future context.
    assert "col-77" in res.deep_results[0].future_context

    # 3. THE SAME DESTINATION as a prose runner: the async card updater's ONE LLM call is given them,
    #    and the edits it returns are written to the card store. Asserted on the updater's actual
    #    prompt, not merely on the field being populated.
    _wait_for_updater(store)
    assert provider.updater_calls == 1
    assert any("col-77" in p and "quest_goals" in p for p in provider.updater_prompts), \
        "the code runner's future context never reached the card updater"
    assert store.update_calls and store.update_calls[0]["card_id"] == "u:u1:goals"


def test_prose_runner_future_context_reaches_the_card_updater_the_same_way():
    """The prose channel feeds the identical destination, now via the same normalized field."""
    provider = CardEditProvider({"edits": [{"card_id": "pricing",
                                            "add": [{"type": "collection",
                                                     "locator": {"name": "Pricing tiers",
                                                                 "id": "col-123"}}]}]})
    store = RecordingCardStore(current_cards=[{"id": "pricing", "title": "Pricing", "files": []}])
    runner = _Runner(_WORKER_OUTPUT)
    orch = _orch(provider, runner, assembler=store)

    res = orch.run("update the pricing docs", context_meta={"user_id": "u1"})

    # The deliverable is kept; the internal section is cut from the payload at the runner seam.
    payload = res.deep_results[0].output
    assert "I implemented the thing and verified it." in payload
    assert FUTURE_CONTEXT_DELIMITER not in payload
    assert "col-123" not in payload
    # ...and now lives in the structured field, which is what the updater reads.
    assert "col-123" in res.deep_results[0].future_context

    _wait_for_updater(store)
    assert provider.updater_calls == 1
    assert any("col-123" in p for p in provider.updater_prompts)
    assert store.update_calls and store.update_calls[0]["card_id"] == "u:u1:pricing"


def test_a_worker_that_appends_the_block_to_code_anyway_is_cleaned_centrally():
    """Structural, not by convention: even if a strict-format worker disobeys and appends the prose
    block, the orchestrator cuts it at the seam, so the payload a consumer receives still parses."""
    provider = CardEditProvider({"edits": [{"card_id": "goals"}]})
    store = RecordingCardStore(current_cards=[{"id": "goals", "title": "Goals", "files": []}])
    runner = _CodeRunner(contaminate=True)
    orch = _orch(provider, runner, assembler=store)

    res = orch.run("add a status mutation", context_meta={"user_id": "u1"})

    payload = res.deep_results[0].output
    ast.parse(payload)
    assert FUTURE_CONTEXT_DELIMITER not in payload
    assert "leaked" not in payload
    # The runner's own field still wins as the thing the updater learns from.
    assert "col-77" in res.deep_results[0].future_context


# --------------------------------------------------------------------------- the normalization seam


def test_normalize_moves_the_section_out_of_a_prose_output():
    res = _normalize_future_context(DeepResult(met=True, output=_WORKER_OUTPUT))
    assert FUTURE_CONTEXT_DELIMITER not in res.output
    assert "I implemented the thing and verified it." in res.output
    assert "col-123" in res.future_context


def test_normalize_leaves_a_clean_field_result_untouched():
    res = _normalize_future_context(
        DeepResult(met=True, output=_GENERATED_CODE, future_context=_CODE_FUTURE_CONTEXT))
    assert res.output == _GENERATED_CODE          # payload byte-for-byte unchanged
    assert res.future_context == _CODE_FUTURE_CONTEXT


def test_normalize_is_inert_when_there_is_nothing_to_move():
    res = _normalize_future_context(DeepResult(met=True, output="plain result"))
    assert res.output == "plain result"
    assert res.future_context == ""
    # A deferred hand-off receipt (the reserved queue-runner path) carries no section and must pass
    # through untouched: the goal loop trusts ``met``/``deferred`` and must still see its sentinel.
    receipt = _normalize_future_context(
        DeepResult(met=True, output="task #7 launched", deferred=True))
    assert receipt.output == "task #7 launched"
    assert receipt.deferred is True
    assert receipt.future_context == ""


def test_deep_future_context_prefers_the_field_and_falls_back_to_parsing():
    # The field is authoritative once normalized...
    assert "col-77" in _deep_future_context(
        DeepResult(met=True, output=_GENERATED_CODE, future_context=_CODE_FUTURE_CONTEXT))
    # ...and a DeepResult built OUTSIDE the seam (an older runner, a queued result reflected back)
    # still teaches the cards, via the in-output parse.
    assert "col-123" in _deep_future_context(DeepResult(met=True, output=_WORKER_OUTPUT))
    assert _deep_future_context(DeepResult(met=True, output="nothing here")) == ""


# --------------------------------------------------------------------------- per-runner routing


def test_the_channel_follows_the_RUNNER_the_classifier_picked_not_the_default():
    """With a named registry, the instruction must match the runner that will actually handle THIS
    goal: a consumer with one prose runner and one code runner must not send the prose ask to the
    code generator."""
    provider = CardEditProvider({"edits": [{"card_id": "goals"}]})
    store = RecordingCardStore(current_cards=[{"id": "goals", "title": "Goals", "files": []}])
    code_runner = _CodeRunner()
    prose_runner = _Runner(_WORKER_OUTPUT)

    orch = Orchestrator(
        retrieval=StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=prose_runner,
        deep_runners={"code": code_runner, "text": prose_runner},
        deep_runner_classifier=lambda user_message, goal, brief: "code",
        config=OrchestratorConfig(deep_goal_max_iterations=2, deep_model_ladder=["sonnet"]),
        context_assembler=store,
    )
    res = orch.run("generate the mutation", context_meta={"user_id": "u1"})

    assert code_runner.briefs and not prose_runner.briefs
    assert all(DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION in b for b in code_runner.briefs)
    assert all(DEEP_FUTURE_CONTEXT_INSTRUCTION not in b for b in code_runner.briefs)
    ast.parse(res.deep_results[0].output)
    assert "col-77" in res.deep_results[0].future_context
