"""Offline tests for TurnContextStore and CompositeContextAssembler."""
import pytest
from quest_ai_runner.core.turn_context_store import TurnContextStore
from quest_ai_runner.core.composite_assembler import CompositeContextAssembler
from quest_ai_runner.core.adapters import AssembledContext


# ---------------------------------------------------------------------------
# TurnContextStore tests
# ---------------------------------------------------------------------------


def test_empty_returns_empty_context_view(tmp_path):
    """No cards on disk -> assemble() returns an AssembledContext with empty context_view."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    result = store.assemble("what is X?")
    assert isinstance(result, AssembledContext)
    assert result.context_view == ""


def test_record_creates_card(tmp_path):
    """After record(), assemble() with a matching query returns context_view containing the user text."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    store.record("what is X?", {"response": "X is a thing"})
    result = store.assemble("X")
    assert "what is X?" in result.context_view


def test_last_turn_always_included(tmp_path):
    """The most recently recorded turn always appears in assemble() regardless of keyword overlap."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_older=0)
    for i in range(5):
        store.record(f"question about topic{i}", {"response": f"answer{i}"})
    # Query is completely unrelated to all recorded turns
    result = store.assemble("zzz completely unrelated xyzzy")
    # The last turn (topic4) must always be present
    assert "topic4" in result.context_view


def test_keyword_overlap_selects_older(tmp_path):
    """An older turn sharing keywords with the current message is included."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_older=4)
    # Turn 1: about python
    store.record("how do I write python tests", {"response": "use pytest"})
    # Turn 2: about database (irrelevant to our query)
    store.record("what is postgres", {"response": "a relational database"})
    # Turn 3: most recent (always included)
    store.record("latest turn unrelated", {"response": "some answer"})

    result = store.assemble("python unit testing frameworks")
    # Turn 3 always included
    assert "latest turn" in result.context_view
    # Turn 1 overlaps with "python testing"
    assert "write python tests" in result.context_view


def test_irrelevant_older_turn_excluded(tmp_path):
    """An older turn with zero keyword overlap is not included (beyond the last turn)."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_older=4)
    # Turn 1: about database
    store.record("what is postgres", {"response": "a relational database"})
    # Turn 2: most recent (always included)
    store.record("latest turn unrelated", {"response": "some answer"})

    # Query is about python -- should NOT pull in the postgres turn
    result = store.assemble("python unit testing frameworks")
    assert "postgres" not in result.context_view


def test_assistant_summary_truncated(tmp_path):
    """Long assistant responses are truncated in the stored summary."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_assistant_chars=20)
    long_response = "a" * 200
    store.record("question", {"response": long_response})
    result = store.assemble("question")
    # The full 200-char response should NOT appear verbatim in the context_view
    assert "a" * 200 not in result.context_view
    # The ellipsis marker should appear
    assert "…" in result.context_view


def test_record_skips_empty_task_and_response(tmp_path):
    """record() with empty task and response creates no card."""
    turns_dir = tmp_path / "turns"
    store = TurnContextStore(turns_dir=str(turns_dir))
    store.record("", {"response": ""})
    # No card should exist
    result = store.assemble("anything")
    assert result.context_view == ""


def test_max_turns_prunes_oldest(tmp_path):
    """When max_turns is exceeded, the oldest cards are pruned."""
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_turns=3)
    for i in range(5):
        store.record(f"question{i}", {"response": f"answer{i}"})
    # After 5 records with max_turns=3, only 3 remain on disk
    cards = list((tmp_path / "turns").glob("*.json"))
    assert len(cards) == 3


# ---------------------------------------------------------------------------
# CompositeContextAssembler tests
# ---------------------------------------------------------------------------


class _FakeAssembler:
    """Minimal ContextAssembler stub for tests."""

    def __init__(self, view: str):
        self._view = view
        self.recorded: list = []

    def assemble(self, task_text: str, *, meta=None) -> AssembledContext:
        return AssembledContext(context_view=self._view)

    def record(self, task_text: str, outcome: dict) -> None:
        self.recorded.append((task_text, outcome))


def test_composite_assembler_concatenates():
    """CompositeContextAssembler concatenates non-empty context_view strings from each assembler."""
    store_a = _FakeAssembler("view from A")
    store_b = _FakeAssembler("view from B")
    composite = CompositeContextAssembler([store_a, store_b])
    result = composite.assemble("some task")
    assert "view from A" in result.context_view
    assert "view from B" in result.context_view


def test_composite_assembler_skips_empty_views():
    """CompositeContextAssembler skips assemblers that return empty context_view."""
    store_a = _FakeAssembler("")
    store_b = _FakeAssembler("view from B")
    composite = CompositeContextAssembler([store_a, store_b])
    result = composite.assemble("some task")
    assert "view from B" in result.context_view
    # No double-newline separator when one view is empty
    assert "\n\n" not in result.context_view


def test_composite_record_calls_both():
    """record() on a CompositeContextAssembler calls record() on each assembler."""
    store_a = _FakeAssembler("A")
    store_b = _FakeAssembler("B")
    composite = CompositeContextAssembler([store_a, store_b])
    composite.record("task", {"response": "answer"})
    assert len(store_a.recorded) == 1
    assert len(store_b.recorded) == 1
    assert store_a.recorded[0] == ("task", {"response": "answer"})
    assert store_b.recorded[0] == ("task", {"response": "answer"})


def test_composite_assembler_merges_card_ids():
    """CompositeContextAssembler merges card_ids from all assemblers."""

    class _IdsAssembler:
        def __init__(self, ids):
            self._ids = ids

        def assemble(self, task_text: str, *, meta=None) -> AssembledContext:
            return AssembledContext(context_view="x", card_ids=self._ids)

        def record(self, task_text: str, outcome: dict) -> None:
            pass

    composite = CompositeContextAssembler([_IdsAssembler(["a", "b"]), _IdsAssembler(["c"])])
    result = composite.assemble("task")
    assert set(result.card_ids) == {"a", "b", "c"}


def test_composite_assembler_tolerates_failing_assembler():
    """If one assembler raises, the composite still returns results from the others."""

    class _FailingAssembler:
        def assemble(self, task_text: str, *, meta=None) -> AssembledContext:
            raise RuntimeError("boom")

        def record(self, task_text: str, outcome: dict) -> None:
            raise RuntimeError("boom")

    store_b = _FakeAssembler("view from B")
    composite = CompositeContextAssembler([_FailingAssembler(), store_b])
    result = composite.assemble("task")
    assert "view from B" in result.context_view
    # record() should also not raise
    composite.record("task", {})


def test_composite_with_real_turn_store(tmp_path):
    """CompositeContextAssembler integrates correctly with a real TurnContextStore."""
    turn_store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    turn_store.record("tell me about python", {"response": "Python is a language"})
    composite = CompositeContextAssembler([_FakeAssembler("file context"), turn_store])
    result = composite.assemble("python programming")
    assert "file context" in result.context_view
    assert "tell me about python" in result.context_view
