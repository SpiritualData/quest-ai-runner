"""Offline tests for TurnMemory -- relevant-turn transcript selection."""
from quest_ai_runner.core.turn_memory import TurnMemory


def test_empty_returns_empty():
    mem = TurnMemory()
    assert mem.relevant_transcript("anything") == ""


def test_recent_always_included():
    mem = TurnMemory(always_recent=2, max_older=0)
    mem.add("first question", "first answer")
    mem.add("second question", "second answer")
    mem.add("third question", "third answer")
    # The message is about something completely unrelated so keyword overlap is zero.
    transcript = mem.relevant_transcript("zzz unrelated xyz")
    # The last two turns must always be present regardless of relevance.
    assert "second question" in transcript
    assert "third question" in transcript


def test_relevant_older_selected():
    mem = TurnMemory(always_recent=1, max_older=2)
    # Turn 1: about "python testing"
    mem.add("how do I write python tests", "use pytest")
    # Turn 2: about "database"
    mem.add("what is postgres", "a relational database")
    # Turn 3: most recent (always included)
    mem.add("latest turn unrelated", "answer")

    # Current message is about python -- should pull in turn 1, not turn 2.
    transcript = mem.relevant_transcript("python unit testing frameworks")
    # Turn 3 is always-recent.
    assert "latest turn" in transcript
    # Turn 1 overlaps with "python testing".
    assert "write python tests" in transcript
    # Turn 2 (database) should NOT be included -- no keyword overlap.
    assert "postgres" not in transcript


def test_clear():
    mem = TurnMemory()
    mem.add("hello", "world")
    mem.add("foo", "bar")
    mem.clear()
    assert mem.relevant_transcript("hello") == ""
    assert mem.turn_count == 0


def test_chronological_order():
    mem = TurnMemory(always_recent=1, max_older=4)
    # All turns share the keyword "python" so they all get selected.
    mem.add("python basics", "learn python first")
    mem.add("python classes", "use class keyword")
    mem.add("python functions", "def keyword")
    # Turn 4 is the most recent (always included).
    mem.add("python decorators", "use at sign")

    transcript = mem.relevant_transcript("python programming")
    # All turns selected; verify they appear in chronological order (basics before classes etc.)
    pos_basics = transcript.find("python basics")
    pos_classes = transcript.find("python classes")
    pos_functions = transcript.find("python functions")
    pos_decorators = transcript.find("python decorators")
    assert pos_basics < pos_classes < pos_functions < pos_decorators


def test_single_turn_always_included():
    mem = TurnMemory(always_recent=2)
    mem.add("only turn", "only answer")
    transcript = mem.relevant_transcript("completely unrelated")
    assert "only turn" in transcript


def test_turn_count():
    mem = TurnMemory()
    assert mem.turn_count == 0
    mem.add("a", "b")
    assert mem.turn_count == 1
    mem.add("c", "d")
    assert mem.turn_count == 2
    mem.clear()
    assert mem.turn_count == 0
