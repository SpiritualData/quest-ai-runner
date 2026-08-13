"""The STRUCTURAL sufficiency gate: an abridged context item is opened before it is answered from.

The failure this reproduces, from a real turn: the assembled context surfaced a card whose content
was a short synthesized SUMMARY of a note whose full text was live-fetchable, and the planner
answered at step 0 anyway, telling the user it had "only the note header" and asking whether it
should go and get the rest. The prose SUFFICIENCY gate in the planner prompt said not to do that;
nothing enforced it.
"""
from typing import Any, Dict, List

from quest_ai_runner.adapters.reference_resolver import NoteResolver
from quest_ai_runner.core.adapters import AssembledContext, Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.sufficiency import (
    AbridgedTurnState,
    collect_abridged_items,
    render_abridged_notice,
    spec_covers,
    unfetched_abridged,
)

from .conftest import StubProvider

FULL_NOTE = "GAP LIST: 1) pricing page has no CTA. 2) onboarding drops at step 3. 3) no refund copy."
SUMMARY = "Notes on the remaining gaps"
FETCH_SPEC: Dict[str, Any] = {
    "query": {"kind": "goal_context", "goal_id": "g1", "quest_id": "q1", "include_notes": True}
}


def _abridged_card() -> Dict[str, Any]:
    """One card whose single content item is a summary that declares how to fetch the real source."""
    return {
        "id": "card_gaps",
        "title": "Remaining gaps",
        "items": [{
            "id": "item_1",
            "type": "note",
            "why": "the gap list for this goal",
            "text": SUMMARY,
            "locator": {"text": SUMMARY, "full_ref": FETCH_SPEC},
            "pointer_eligible": False,
        }],
    }


class StubAssembler:
    """A ContextAssembler that hands back one card carrying an abridged note item."""

    def __init__(self, cards: List[Dict[str, Any]]):
        self.cards = cards
        self.calls: List[str] = []

    def assemble(self, task_text: str, meta: Dict[str, Any] | None = None) -> AssembledContext:
        self.calls.append(task_text)
        view_lines = []
        for c in self.cards:
            view_lines.append(f"## {c.get('title') or c.get('id')}")
            for it in c.get("items", []):
                view_lines.append(f"  - (note) {it.get('why', '')}")
                view_lines.append(f"      {it.get('text', '')}")
        return AssembledContext(context_view="\n".join(view_lines),
                                card_ids=[c["id"] for c in self.cards],
                                card_metadata=list(self.cards))

    def record(self, *a: Any, **kw: Any) -> None:  # pragma: no cover - not used by these tests
        return None


class RecordingRetrieval:
    """A RetrievalAdapter that records every query spec and serves the full note for the fetch."""

    def __init__(self, fail: bool = False):
        self.query_specs: List[Dict[str, Any]] = []
        self.fail = fail

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None,
                     max_bytes=None):
        return Observation(kind="error", rel_path=rel_path, error="not found")

    def grep(self, pattern, *, scope=None, max_hits=None):
        return Observation(kind="grep", pattern=pattern, scope=scope, hits=[])

    def query(self, spec):
        self.query_specs.append(dict(spec))
        if self.fail:
            return Observation(kind="error", error="upstream unavailable")
        return Observation(kind="query", locator="goal_context", text=FULL_NOTE)


def _orch(provider, retrieval, assembler, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), context_assembler=assembler, **kw)


# --- the unit pieces -------------------------------------------------------------------------

def test_collects_only_items_that_declare_a_usable_fetch():
    plain = {"id": "c2", "items": [{"id": "i", "type": "note", "locator": {"text": "just a fact"}}]}
    junk = {"id": "c3", "items": [{"id": "i", "type": "note",
                                   "locator": {"text": "x", "full_ref": {"nonsense": 1}}}]}
    items = collect_abridged_items([_abridged_card(), plain, junk])
    assert len(items) == 1
    assert items[0].fetch == FETCH_SPEC
    assert items[0].chars == len(SUMMARY)
    assert items[0].card_id == "card_gaps"


def test_same_source_on_two_cards_forces_one_read():
    a, b = _abridged_card(), _abridged_card()
    b["id"] = "card_other"
    assert len(collect_abridged_items([a, b])) == 1


def test_notice_names_the_fetch_and_forbids_asking_permission():
    notice = render_abridged_notice(collect_abridged_items([_abridged_card()]))
    assert "goal_context" in notice
    assert "SUMMARY" in notice
    assert "do NOT ask the user" in notice
    assert render_abridged_notice([]) == ""


def test_spec_coverage_matches_the_nested_and_flat_shapes():
    flat = {"kind": "goal_context", "goal_id": "g1", "quest_id": "q1", "include_notes": True}
    assert spec_covers(flat, FETCH_SPEC)                      # adapters see the flattened shape
    assert spec_covers({**flat, "limit": 20}, FETCH_SPEC)     # extra filters still cover it
    assert not spec_covers({"kind": "goal_context", "goal_id": "OTHER"}, FETCH_SPEC)
    assert not spec_covers({"rel_path": "README.md"}, FETCH_SPEC)


def test_unfetched_is_computed_from_reads_that_actually_ran():
    items = collect_abridged_items([_abridged_card()])
    assert unfetched_abridged(items, []) == items
    assert unfetched_abridged(items, [FETCH_SPEC]) == []
    state = AbridgedTurnState(items=items)
    assert state.should_force_read() == items
    state.record_reads([FETCH_SPEC])
    assert state.should_force_read() == []


def test_note_resolver_marks_an_abridged_note_and_leaves_a_plain_one_alone():
    r = NoteResolver()
    plain = r.resolve({"text": "a durable fact"})
    assert plain == "a durable fact"                          # unchanged, as before
    marked = r.resolve({"text": SUMMARY, "full_ref": FETCH_SPEC})
    assert marked.startswith(SUMMARY)
    assert "abridged" in marked and "goal_context" in marked
    # The marker survives the char cap: the text is what gets trimmed, never the warning.
    tight = r.resolve({"text": "x" * 500, "full_ref": FETCH_SPEC}, max_chars=200)
    assert "abridged" in tight and len(tight) <= 200


# --- the orchestrator gate -------------------------------------------------------------------

def test_forces_the_full_read_instead_of_answering_from_the_summary():
    # The planner would answer immediately, twice over. The gate turns the FIRST one into a read.
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "I have the note header"},
        {"action": "answer", "model_tier": "sonnet", "rationale": "now I have the gap list"},
        {"met": True, "reason": "answered from the full note"},
    ])
    retrieval = RecordingRetrieval()
    res = _orch(provider, retrieval, StubAssembler([_abridged_card()])).run("what should I do next")

    assert res.kind == "answer"
    # It actually went and got the full text, using the spec the item itself declared.
    assert retrieval.query_specs, "the declared full_ref fetch was never executed"
    assert retrieval.query_specs[0].get("kind") == "goal_context"
    assert retrieval.query_specs[0].get("goal_id") == "g1"
    # ... and the answer was grounded in what came back, not in the summary.
    joined = "\n".join(str(m["content"]) for m in provider.last_answer_messages)
    assert FULL_NOTE in joined
    # The planner was also TOLD which item was a summary and how to open it.
    assert "ABRIDGED CONTEXT ITEMS" in provider.plan_prompts[0]


def test_no_forced_read_when_the_planner_already_fetched_it():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"query": {"kind": "goal_context", "goal_id": "g1",
                                                "quest_id": "q1", "include_notes": True}}],
         "rationale": "pull the notes"},
        {"action": "answer", "rationale": "have the gap list"},
        {"met": True, "reason": "ok"},
    ])
    retrieval = RecordingRetrieval()
    res = _orch(provider, retrieval, StubAssembler([_abridged_card()])).run("what should I do next")
    assert res.kind == "answer"
    assert len(retrieval.query_specs) == 1  # the gate added nothing on top of the planner's own read


def test_gate_is_inert_for_a_card_that_declares_nothing():
    plain = {"id": "c", "title": "Facts",
             "items": [{"id": "i", "type": "note", "why": "a fact", "text": "the fact",
                        "locator": {"text": "the fact"}}]}
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat"},
        {"met": True, "reason": "ok"},
    ])
    retrieval = RecordingRetrieval()
    res = _orch(provider, retrieval, StubAssembler([plain])).run("hey there")
    assert res.kind == "answer"
    assert retrieval.query_specs == []
    assert "ABRIDGED CONTEXT ITEMS" not in provider.plan_prompts[0]


def test_a_failed_fetch_does_not_loop_the_turn():
    # The fetch is attempted ONCE. If the source is down, the turn still answers (honestly, with
    # the failure in GATHERED) rather than re-forcing the same read forever.
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "answer now"},
        {"action": "answer", "rationale": "answer anyway"},
        {"met": True, "reason": "ok"},
    ])
    retrieval = RecordingRetrieval(fail=True)
    res = _orch(provider, retrieval, StubAssembler([_abridged_card()])).run("what should I do next")
    assert res.kind == "answer"
    assert len(retrieval.query_specs) == 1


def test_the_gate_can_be_turned_off():
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "answer from the summary"},
        {"met": True, "reason": "ok"},
    ])
    retrieval = RecordingRetrieval()
    orch = _orch(provider, retrieval, StubAssembler([_abridged_card()]),
                 config=OrchestratorConfig(full_read_before_answer=False))
    res = orch.run("what should I do next")
    assert res.kind == "answer"
    assert retrieval.query_specs == []
