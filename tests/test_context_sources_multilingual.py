"""Verify that both QAR turn cards and Claude session files surface relevant
context for a query about a distinctive topic (multilingual support).

Two separate retrieval paths are tested:
  1. TurnContextStore.assemble() — pre-flight context injection from prior QAR turns.
  2. ClaudeConversationsAdapter.grep() — reactive search across Claude session files.

Both are expected to surface the prior multilingual conversation even when other
unrelated turns/sessions are present and the common words in the query ("questions",
"need", "answer") appear in many other cards.
"""
import json
from pathlib import Path

import pytest

from quest_ai_runner.adapters import ClaudeConversationsAdapter
from quest_ai_runner.core.turn_context_store import TurnContextStore

QUERY = "What questions do I need to answer about multilingual support?"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def turn_store_with_history(tmp_path):
    """TurnContextStore pre-populated with a mix of turns.

    The distinctive turn is the one about multilingual support; everything else
    uses common words that overlap with the query (questions, need, answer, support)
    so a raw overlap count would bury the multilingual card.
    """
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), max_older=4)

    # Many generic turns that share common query words (questions, need, answer, support).
    for i in range(10):
        store.record(
            f"I need to answer questions about topic{i}",
            {"response": f"Here are the answers for topic{i} support."},
        )

    # The distinctive prior turn — rare term "multilingual" should rank it highly.
    store.record(
        "Let me ask you some questions about multilingual support for the NLP pipeline",
        {"response": (
            "We need to decide: do we translate the UI only, or do the AI models "
            "respond in the user's native language? That changes prompt templates "
            "and token costs for multilingual users."
        )},
    )

    # A few more generic turns after to confirm recency doesn't dominate over relevance.
    for i in range(3):
        store.record(
            f"follow-up question about deployment{i}",
            {"response": f"The deployment answer{i} for support."},
        )

    return store


@pytest.fixture()
def claude_sessions_dir(tmp_path):
    """Claude sessions directory with a mix of session files.

    One session discusses multilingual support; others share common query words.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    # Generic sessions that share common words with the query.
    for i in range(5):
        (sessions / f"generic_{i}.json").write_text(json.dumps({
            "messages": [
                {"role": "user", "text": f"I need to answer questions about topic{i}"},
                {"role": "assistant", "text": f"Here is the answer for support case {i}."},
            ]
        }))

    # The distinctive session with "multilingual".
    (sessions / "multilingual_planning.json").write_text(json.dumps({
        "messages": [
            {
                "role": "user",
                "text": "What are the open questions about multilingual support for Quest?",
            },
            {
                "role": "assistant",
                "text": (
                    "Key decisions: language detection strategy, whether AI models reply "
                    "in the user's language, and how prompt templates change for multilingual users."
                ),
            },
        ]
    }))

    return sessions


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_turn_store_surfaces_multilingual_card(turn_store_with_history):
    """TurnContextStore must inject the multilingual turn even when 10+ other turns
    share common query words like 'questions', 'need', 'answer', 'support'.
    """
    result = turn_store_with_history.assemble(QUERY)

    assert result.context_view, "assemble() returned empty context"
    assert "multilingual" in result.context_view, (
        "The multilingual turn was not surfaced — TF-DF-IDF scoring may not be weighting "
        "the rare term 'multilingual' highly enough relative to common words."
    )


def test_turn_store_does_not_surface_only_generic_cards(turn_store_with_history):
    """The generic turns should not crowd out the multilingual card."""
    result = turn_store_with_history.assemble(QUERY)
    # 'topic0'..'topic9' content should not fill all slots at the expense of multilingual.
    assert "multilingual" in result.context_view


def test_claude_sessions_grep_finds_multilingual(claude_sessions_dir):
    """ClaudeConversationsAdapter.grep() must find the multilingual session."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(claude_sessions_dir))
    result = adapter.grep("multilingual")

    assert result.hits, "grep returned no hits for 'multilingual'"
    hit_text = " ".join(str(h) for h in result.hits).lower()
    assert "multilingual" in hit_text


def test_claude_sessions_grep_does_not_match_unrelated(claude_sessions_dir):
    """A term absent from all sessions returns no hits."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(claude_sessions_dir))
    result = adapter.grep("zzz_nonexistent_term_xyzzy")
    assert not result.hits


def test_claude_sessions_assemble_surfaces_multilingual(claude_sessions_dir):
    """ClaudeConversationsAdapter.assemble() must inject the multilingual session
    as pre-flight context without the planner having to call grep first.
    """
    adapter = ClaudeConversationsAdapter(sessions_dir=str(claude_sessions_dir))
    result = adapter.assemble(QUERY)

    assert result.context_view, "assemble() returned empty context"
    assert "multilingual" in result.context_view.lower(), (
        "The multilingual session was not surfaced in pre-flight context — "
        "TF-DF-IDF keyword scoring via select_representatives may not be ranking it."
    )
