"""Test suite for CompositeRetrievalAdapter and ClaudeConversationsAdapter."""
import json
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.adapters import (
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
    FilesAdapter,
)


@pytest.fixture
def temp_corpus():
    """Create a temporary corpus with test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create some test files
        (tmppath / "docs").mkdir()
        (tmppath / "docs" / "design.md").write_text(
            "# Design\n\nWe use a pattern-based architecture.\n"
        )
        (tmppath / "code").mkdir()
        (tmppath / "code" / "example.py").write_text(
            "# Example pattern implementation\ndef handler():\n    pass\n"
        )

        yield tmppath


@pytest.fixture
def temp_sessions():
    """Create a temporary Claude sessions directory with test conversations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create mock session files
        session1 = {
            "rep_name": "Assistant",
            "messages": [
                {"role": "user", "text": "How do we handle patterns?"},
                {"role": "assistant", "text": "We use a pattern-based approach."},
            ],
        }
        session2 = {
            "rep_name": "Assistant",
            "messages": [
                {"role": "user", "text": "What about error handling?"},
                {"role": "assistant", "text": "We validate at boundaries."},
            ],
        }

        (tmppath / "design_discussion.json").write_text(json.dumps(session1))
        (tmppath / "error_handling.json").write_text(json.dumps(session2))

        yield tmppath


def test_claude_conversations_adapter_loads_sessions(temp_sessions):
    """Test that ClaudeConversationsAdapter loads session files."""
    # Test with explicit sessions_dir
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    # Check that both sessions were loaded
    assert adapter._conversations
    assert "design_discussion" in adapter._conversations
    assert "error_handling" in adapter._conversations


def test_claude_conversations_adapter_read_section(temp_sessions):
    """Test reading a conversation section."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    obs = adapter.read_section("design_discussion")
    assert obs.kind == "read"
    assert "pattern" in obs.text.lower()
    assert "user:" in obs.text.lower()
    assert "assistant:" in obs.text.lower()


def test_claude_conversations_adapter_grep(temp_sessions):
    """Test grepping across conversations."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    obs = adapter.grep("pattern")
    assert obs.kind == "grep"
    assert obs.hits
    assert any("pattern" in str(hit).lower() for hit in obs.hits)


def test_claude_conversations_adapter_list_sources(temp_sessions):
    """Test listing available conversations."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    obs = adapter.list_sources()
    assert obs.kind == "query"
    assert "design_discussion" in obs.text
    assert "error_handling" in obs.text


def test_composite_adapter_single_source(temp_corpus):
    """Test CompositeRetrievalAdapter with one source."""
    files = FilesAdapter(str(temp_corpus))
    composite = CompositeRetrievalAdapter([files])

    obs = composite.read_section("docs/design.md")
    assert obs.kind == "read"
    assert "pattern" in obs.text.lower()


def test_composite_adapter_multiple_sources(temp_corpus, temp_sessions):
    """Test CompositeRetrievalAdapter with multiple sources in parallel."""
    files = FilesAdapter(str(temp_corpus))
    conversations = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))
    composite = CompositeRetrievalAdapter([files, conversations])

    # Read from files should work
    obs = composite.read_section("docs/design.md")
    assert obs.kind == "read"
    assert "pattern" in obs.text.lower()

    # Read from conversations should work (try any available)
    if conversations._conversations:
        conv_id = next(iter(conversations._conversations.keys()))
        obs = composite.read_section(conv_id)
        assert obs.kind == "read"


def test_composite_adapter_grep_parallel(temp_corpus, temp_sessions):
    """Test parallel grep across multiple sources."""
    files = FilesAdapter(str(temp_corpus))
    conversations = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))
    composite = CompositeRetrievalAdapter([files, conversations], max_workers=2)

    obs = composite.grep("pattern")
    assert obs.kind == "grep"
    # Should find hits from both files and conversations
    assert obs.hits
    assert len(obs.hits) >= 2  # at least from both sources


def test_composite_adapter_list_sources_merged(temp_corpus, temp_sessions):
    """Test that list_sources merges results from all adapters."""
    files = FilesAdapter(str(temp_corpus))
    conversations = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))
    composite = CompositeRetrievalAdapter([files, conversations])

    obs = composite.list_sources()
    assert obs.kind == "query"
    # Should list conversation sources (FilesAdapter may return a description)
    assert "design_discussion" in obs.text
    assert "error_handling" in obs.text


def test_composite_adapter_handles_missing_paths(temp_corpus, temp_sessions):
    """Test that composite adapter degrades gracefully for missing paths."""
    files = FilesAdapter(str(temp_corpus))
    conversations = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))
    composite = CompositeRetrievalAdapter([files, conversations])

    # Try to read a path that doesn't exist
    obs = composite.read_section("nonexistent.txt")
    assert obs.kind == "error"
    assert "not found" in obs.error.lower()


def test_claude_conversations_adapter_corpus_root(temp_corpus, temp_sessions):
    """Test ClaudeConversationsAdapter with corpus_root parameter."""
    # Create a corpus-like structure with conversations subdirectory
    import shutil

    conversations_dir = temp_corpus / "conversations"
    conversations_dir.mkdir()
    for session_file in temp_sessions.glob("*.json"):
        shutil.copy(session_file, conversations_dir / session_file.name)

    # Test with corpus_root (should auto-discover corpus/conversations/)
    adapter = ClaudeConversationsAdapter(corpus_root=str(temp_corpus))

    assert adapter._conversations
    assert "conversations:design_discussion" in adapter._conversations
    assert "conversations:error_handling" in adapter._conversations


def test_claude_conversations_adapter_recursive_discovery(temp_corpus, temp_sessions):
    """Test that ClaudeConversationsAdapter recursively finds conversations in .claude and conversations/ dirs."""
    import shutil

    # Create conversations in multiple nested locations
    (temp_corpus / "docs" / ".claude").mkdir(parents=True)
    (temp_corpus / "code" / "conversations").mkdir(parents=True)

    # Copy some conversations to different places
    for session_file in list(temp_sessions.glob("*.json"))[:1]:
        shutil.copy(session_file, temp_corpus / "docs" / ".claude" / session_file.name)
    for session_file in list(temp_sessions.glob("*.json"))[1:]:
        shutil.copy(session_file, temp_corpus / "code" / "conversations" / session_file.name)

    # Should find conversations in nested .claude and conversations/ directories
    adapter = ClaudeConversationsAdapter(corpus_root=str(temp_corpus))

    assert adapter._conversations
    # Conversations in .claude dir
    assert any("docs:.claude:" in cid for cid in adapter._conversations.keys())
    # Conversations in conversations dir
    assert any("code:conversations:" in cid for cid in adapter._conversations.keys())
