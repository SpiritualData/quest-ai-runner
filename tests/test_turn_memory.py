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


def test_default_always_recent_is_one():
    """Default TurnMemory() includes exactly 1 recent turn (always_recent=1)."""
    mem = TurnMemory(max_older=0)  # disable older selection to isolate always-recent behavior
    mem.add("first question", "first answer")
    mem.add("second question", "second answer")
    mem.add("third question", "third answer")
    # With always_recent=1 and max_older=0, only the last turn should be present.
    transcript = mem.relevant_transcript("zzz unrelated xyz")
    assert "third question" in transcript
    assert "second question" not in transcript
    assert "first question" not in transcript


def test_max_assistant_chars_truncates():
    """Rendered transcript truncates assistant text to max_assistant_chars with ellipsis."""
    mem = TurnMemory(max_assistant_chars=10)
    mem.add("question", "a" * 50)
    transcript = mem.relevant_transcript("question")
    # The assistant line should be truncated to 10 chars + ellipsis
    assert "Assistant: " + "a" * 10 + "…" in transcript
    # The full 50-char response must NOT appear in the rendered transcript
    assert "a" * 50 not in transcript


def test_max_assistant_chars_zero_disables_truncation():
    """max_assistant_chars=0 disables truncation and the full text appears."""
    long_answer = "b" * 1000
    mem = TurnMemory(max_assistant_chars=0)
    mem.add("question", long_answer)
    transcript = mem.relevant_transcript("question")
    assert long_answer in transcript
    assert "…" not in transcript


def test_max_assistant_chars_keyword_extraction_uses_full_text():
    """Keyword extraction for relevance scoring uses the full assistant text, not the truncated form."""
    mem = TurnMemory(always_recent=1, max_older=2, max_assistant_chars=10)
    # Turn 1: assistant answer contains a keyword buried past char 10
    mem.add("something unrelated", "short" + " " * 20 + "pythonkeyword extra text here")
    # Turn 2 is the always-recent anchor
    mem.add("anchor turn", "anchor answer")

    # Query with the keyword that appears after position 10 in turn 1's assistant text.
    # If keyword extraction used the truncated form (only first 10 chars), this turn
    # would score 0 overlap. With the full text it should score > 0 and be included.
    transcript = mem.relevant_transcript("pythonkeyword")
    assert "something unrelated" in transcript
