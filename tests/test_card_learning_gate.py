"""A card may record what a turn OBSERVED, never what it CLAIMED.

The end-of-turn card updater turned the assistant's own false claims into durable facts, which then
grounded later turns. Two real card contents, both false: one said a goal had been added for a
period although the turn's generated program ran a write that matched no document, and one recorded
a "definitive summary" of a person's project although the turn's reads had returned no match and the
answer was invented.

These tests pin the structural gate (``quest_ai_runner.core.card_learning``) and its use by the
orchestrator's updater: receipt-backed learning is kept, a claim with no observation behind it is
dropped, an unverified turn teaches nothing, and a runner that cannot report observations behaves
exactly as before. Nothing here (and nothing in the gate) reads the WORDS of a model's output.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext, DeepResult
from quest_ai_runner.core.card_learning import (
    NO_OBSERVATIONS_TEXT,
    narrow_edits_to_removals,
    read_observation_lines,
    render_observations_block,
    turn_observations,
    turn_teaches_new_facts,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    CARD_UPDATE_NOTHING_LEARNABLE,
    Orchestrator,
    OrchestratorConfig,
)

from .conftest import StubRetrieval


# --------------------------------------------------------------------------- fixtures


def landed(observations: List[str]) -> DeepResult:
    """A verified run whose receipts show what landed."""
    return DeepResult(met=True, output="done", observations=list(observations),
                      observations_reported=True)


def nothing_landed() -> DeepResult:
    """A run whose write receipts show no change landed (the zero-documents-matched case)."""
    return DeepResult(met=False, output="I added it.", observations=[],
                      observations_reported=True, changed_nothing=True)


def read_nothing() -> DeepResult:
    """A reporting run that read nothing back (the invented-summary case)."""
    return DeepResult(met=True, output="Definitive summary of the project.", observations=[],
                      observations_reported=True)


def legacy_run() -> DeepResult:
    """A runner that cannot report observations at all (e.g. a subprocess agent)."""
    return DeepResult(met=True, output="edited three files")


REAL_READ = {"kind": "read", "rel_path": "notes/plan.md", "locator": "notes/plan.md",
             "text": "The archive migration runs every Tuesday at 06:00."}
EMPTY_READ = {"kind": "read", "rel_path": "notes/plan.md", "locator": "notes/plan.md", "text": ""}
MENU = {"kind": "query", "locator": "sources", "text": "a, b, c", "discovery": True}

ADD_EDIT = {"card_id": "project-notes", "name": "Project notes",
            "add": [{"type": "note", "locator": {"text": "the budget is fixed"}}]}
REMOVE_EDIT = {"card_id": "project-notes", "remove": ["item-7"]}


# --------------------------------------------------------------------------- the gate


def test_receipt_backed_turn_teaches():
    assert turn_teaches_new_facts([landed(["Added the goal"])], []) is True
    assert turn_observations([landed(["Added the goal"])], []) == ["Added the goal"]


def test_write_whose_receipts_show_nothing_landed_teaches_nothing():
    assert turn_teaches_new_facts([nothing_landed()], [REAL_READ]) is False
    # Even a verified run may not teach while its own receipts say nothing changed.
    res = nothing_landed()
    res.met = True
    assert turn_teaches_new_facts([res], [REAL_READ]) is False


def test_reporting_run_with_no_observation_teaches_nothing():
    assert turn_teaches_new_facts([read_nothing()], []) is False
    assert turn_teaches_new_facts([read_nothing()], [EMPTY_READ]) is False
    assert turn_teaches_new_facts([read_nothing()], [MENU]) is False
    # A read that really returned content is support, so the same run may teach.
    assert turn_teaches_new_facts([read_nothing()], [REAL_READ]) is True


def test_not_met_or_unverified_turn_teaches_nothing():
    unverified = DeepResult(met=False, error="Unverified: goal verification did not run",
                            observations=["Added the goal"], observations_reported=True)
    assert turn_teaches_new_facts([unverified], [REAL_READ]) is False
    not_met = DeepResult(met=False, observations=["Added the goal"], observations_reported=True)
    assert turn_teaches_new_facts([not_met], []) is False
    # One not-met run among several is enough to stop the turn from teaching.
    assert turn_teaches_new_facts([landed(["Added the goal"]), not_met], []) is False


def test_a_run_waiting_on_a_human_decision_is_not_a_verdict_against_it():
    # Nothing was judged and nothing failed: the work is parked for a person. Such a run may still
    # teach what it observed on the way, and its own receipt is what says nothing changed yet.
    parked = DeepResult(met=False, decision_id="dec-1", output="Approve this change?",
                        observations=["The change was parked on an approval card. "
                                      "Nothing has changed yet."],
                        observations_reported=True)
    assert turn_teaches_new_facts([parked], []) is True
    # It still needs an observation behind it.
    silent = DeepResult(met=False, decision_id="dec-1", observations=[],
                        observations_reported=True)
    assert turn_teaches_new_facts([silent], []) is False


def test_runner_that_cannot_report_behaves_as_before():
    # No observation channel at all: absence of an observation is not evidence that nothing
    # happened, so the turn is left exactly as it was before the gate existed.
    assert turn_teaches_new_facts([legacy_run()], []) is True
    assert turn_teaches_new_facts([legacy_run()], [REAL_READ]) is True
    # The verdict still governs it.
    assert turn_teaches_new_facts([DeepResult(met=False)], []) is False
    # And so does a reported no-change receipt.
    assert turn_teaches_new_facts([DeepResult(met=True, changed_nothing=True)], []) is False


def test_turn_with_no_deep_run_teaches_nothing():
    assert turn_teaches_new_facts([], [REAL_READ]) is False
    assert turn_teaches_new_facts(None, None) is False


# --------------------------------------------------------------------------- read evidence


def test_read_observation_lines_keep_only_real_content():
    lines = read_observation_lines([REAL_READ, EMPTY_READ, MENU,
                                    {"kind": "error", "error": "boom"},
                                    {"kind": "grep", "pattern": "budget", "hits": []},
                                    {"kind": "grep", "pattern": "budget", "scope": "notes",
                                     "hits": [{"rel_path": "a.md", "line_no": 3,
                                               "line": "budget: fixed"}]},
                                    "not a dict"])
    assert len(lines) == 2
    assert "notes/plan.md" in lines[0] and "Tuesday" in lines[0]
    assert "GREP" in lines[1] and "1 hit" in lines[1]


def test_read_observation_lines_are_bounded():
    many = [dict(REAL_READ, text="x" * 2000) for _ in range(40)]
    lines = read_observation_lines(many, max_lines=5, max_chars=50)
    assert len(lines) == 5
    assert all(len(line) < 120 for line in lines)


def test_render_observations_block_states_when_there_is_nothing():
    assert render_observations_block([]) == NO_OBSERVATIONS_TEXT
    assert render_observations_block(None) == NO_OBSERVATIONS_TEXT
    block = render_observations_block(["Added the goal", "READ notes/plan.md: ..."])
    assert block.startswith("- Added the goal")
    # Bounded, keeping the earliest (most authoritative) lines.
    bounded = render_observations_block(["a" * 400, "b" * 400, "c" * 400], max_chars=500)
    assert "aaa" in bounded and "ccc" not in bounded
    assert "omitted" in bounded


# --------------------------------------------------------------------------- narrowing


def test_narrowing_keeps_only_removals():
    kept = narrow_edits_to_removals([ADD_EDIT, REMOVE_EDIT,
                                     {"card_id": "x", "replace": [{"item_id": "i", "item": {}}]},
                                     {"card_id": "", "remove": ["i"]},
                                     {"remove": ["i"]},
                                     "junk"])
    assert kept == [{"card_id": "project-notes", "remove": ["item-7"]}]
    # The caller's own edits are never mutated.
    assert "name" in ADD_EDIT
    assert narrow_edits_to_removals([]) == []
    assert narrow_edits_to_removals(None) == []


def test_narrowing_keeps_a_removal_that_rides_along_with_additions():
    both = {"card_id": "c1", "name": "New name", "add": [{"type": "note"}], "remove": ["bad-1"]}
    assert narrow_edits_to_removals([both]) == [{"card_id": "c1", "remove": ["bad-1"]}]


# --------------------------------------------------------------------------- through the updater


class RecordingStore:
    """A card-update-capable store (duck-typed: ``update_card`` + ``add_content``) that is also the
    wired ContextAssembler, so the updater is shown ``current`` as this user's current cards."""

    def __init__(self, current: Optional[List[Dict[str, Any]]] = None):
        self.current = current or []
        self.calls: List[Dict[str, Any]] = []

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext(context_view="", card_metadata=list(self.current))

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None

    def add_content(self, card_id: str, item: Dict[str, Any]) -> bool:
        return True

    def update_card(self, card_id: str, **kw: Any) -> bool:
        self.calls.append({"card_id": card_id, **kw})
        return True


class ScriptedProvider:
    """Returns one scripted edit plan for the card updater; records the prompts it was given."""

    def __init__(self, edits: Dict[str, Any]):
        self.edits = edits
        self.prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "card_edits":
            self.prompts.append(prompt)
            return self.edits
        return {"action": "answer"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6", "claude-haiku-4-5"]


def build(provider, store) -> Orchestrator:
    return Orchestrator(
        retrieval=StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        config=OrchestratorConfig(async_card_update=True),
        context_assembler=store,
    )


def test_updater_writes_a_receipt_backed_edit():
    provider = ScriptedProvider({"edits": [ADD_EDIT]})
    store = RecordingStore()
    written = build(provider, store)._update_cards_after_deep(
        request="add the goal", executed="RESULT:\nI added it.", future_context="- the goal",
        ctx_meta=None, observations=["Added the goal"], teaches_new_facts=True)
    assert written == 1
    assert store.calls and store.calls[0]["card_id"] == "project-notes"
    # The observations are handed to the model as the material for a fact.
    assert "Added the goal" in provider.prompts[0]
    assert CARD_UPDATE_NOTHING_LEARNABLE not in provider.prompts[0]


def test_updater_drops_a_claim_only_edit():
    # The turn's receipts show nothing landed, so the proposed "Goal added" item is not applied.
    provider = ScriptedProvider({"edits": [ADD_EDIT]})
    store = RecordingStore(current=[{"id": "project-notes", "title": "Project notes"}])
    written = build(provider, store)._update_cards_after_deep(
        request="add the goal", executed="RESULT:\nI added it.", future_context="- the goal",
        ctx_meta=None, observations=[], teaches_new_facts=False)
    assert written == 0
    assert store.calls == []
    assert CARD_UPDATE_NOTHING_LEARNABLE in provider.prompts[0]
    assert NO_OBSERVATIONS_TEXT in provider.prompts[0]
    # One call only: an empty answer is the right answer here, so it is not retried.
    assert len(provider.prompts) == 1


def test_updater_still_removes_a_wrong_item_on_a_turn_that_teaches_nothing():
    provider = ScriptedProvider({"edits": [dict(ADD_EDIT, remove=["item-7"])]})
    store = RecordingStore(current=[{"id": "project-notes", "title": "Project notes"}])
    written = build(provider, store)._update_cards_after_deep(
        request="that is wrong", executed="RESULT:\n...", future_context="",
        ctx_meta=None, observations=[], teaches_new_facts=False)
    assert written == 1
    call = store.calls[0]
    assert call["remove"] == ["item-7"]
    assert not call.get("add") and not call.get("fields")


def test_updater_makes_no_call_when_nothing_is_learnable_and_no_card_exists():
    provider = ScriptedProvider({"edits": [ADD_EDIT]})
    store = RecordingStore(current=[])
    written = build(provider, store)._update_cards_after_deep(
        request="add the goal", executed="RESULT:\nI added it.", future_context="",
        ctx_meta=None, observations=[], teaches_new_facts=False)
    assert written == 0
    assert provider.prompts == []
    assert store.calls == []
