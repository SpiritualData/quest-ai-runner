"""Test suite for CompositeRetrievalAdapter and ClaudeConversationsAdapter."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.adapters import (
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
    FilesAdapter,
)


def test_claude_conversation_detection():
    """Test that _is_claude_conversation correctly identifies valid conversations."""
    # Valid Claude conversation
    valid = {
        "messages": [
            {"role": "user", "text": "Hello"},
            {"role": "assistant", "text": "Hi"},
        ]
    }
    assert ClaudeConversationsAdapter._is_claude_conversation(valid)

    # Valid with turns instead of messages
    valid_turns = {
        "turns": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
    }
    assert ClaudeConversationsAdapter._is_claude_conversation(valid_turns)

    # Invalid: missing role
    invalid_no_role = {
        "messages": [{"text": "Hello"}]
    }
    assert not ClaudeConversationsAdapter._is_claude_conversation(invalid_no_role)

    # Invalid: missing text/content
    invalid_no_text = {
        "messages": [{"role": "user"}]
    }
    assert not ClaudeConversationsAdapter._is_claude_conversation(invalid_no_text)

    # Invalid: not a dict
    assert not ClaudeConversationsAdapter._is_claude_conversation([])
    assert not ClaudeConversationsAdapter._is_claude_conversation("string")

    # Invalid: random JSON
    random_json = {"name": "test", "value": 123}
    assert not ClaudeConversationsAdapter._is_claude_conversation(random_json)


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

    adapter._ensure_loaded()
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

    adapter._ensure_loaded()
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

    adapter._ensure_loaded()
    assert adapter._conversations
    # Conversations in .claude dir
    assert any("docs:.claude:" in cid for cid in adapter._conversations.keys())
    # Conversations in conversations dir
    assert any("code:conversations:" in cid for cid in adapter._conversations.keys())


def test_conversation_filepath_tracking(temp_sessions):
    """Test that filepaths are tracked for each conversation."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    adapter._ensure_loaded()
    # Check that filepaths are stored
    assert adapter._conversation_filepaths
    assert "design_discussion" in adapter._conversation_filepaths
    assert "error_handling" in adapter._conversation_filepaths

    # Filepaths should be absolute and resolvable
    for conv_id, filepath in adapter._conversation_filepaths.items():
        assert filepath.is_absolute()
        assert filepath.exists()
        assert filepath.suffix == ".json"


def test_conversation_digest_extraction(temp_sessions):
    """Test that digests are correctly extracted from conversations."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))
    adapter._ensure_loaded()
    conv = adapter._conversations["design_discussion"]

    digest = adapter._get_conversation_digest(conv)

    # Digest should contain first message and metadata
    assert "How do we handle patterns?" in digest or "patterns" in digest.lower()
    assert "START:" in digest or "RECENT:" in digest
    assert digest  # Non-empty


def test_conversation_timestamp_extraction(temp_sessions):
    """Test that timestamps are extracted correctly."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    # Create a conversation with a timestamp
    conv_with_ts = {
        "messages": [{"role": "user", "text": "test"}],
        "updated_at": 1234567890.5
    }

    ts = adapter._get_conversation_timestamp(conv_with_ts)
    assert ts == 1234567890.5

    # Conversation without timestamp should return 0
    conv_no_ts = {"messages": [{"role": "user", "text": "test"}]}
    ts = adapter._get_conversation_timestamp(conv_no_ts)
    assert ts == 0.0


def test_conversation_clustering_small_set(temp_sessions):
    """Test clustering with a small number of conversations."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    adapter._ensure_loaded()
    conv_ids = list(adapter._conversations.keys())
    digests = {cid: adapter._get_conversation_digest(adapter._conversations[cid]) for cid in conv_ids}
    timestamps = {cid: adapter._get_conversation_timestamp(adapter._conversations[cid]) for cid in conv_ids}

    sampled = adapter._cluster_and_sample(conv_ids, digests, timestamps, max_clusters=2, samples_per_cluster=1)

    # Should return some conversations
    assert sampled
    # Shouldn't return more than needed
    assert len(sampled) <= len(conv_ids)


def test_claude_conversations_adapter_read_by_filename_with_corpus_root(temp_corpus, temp_sessions):
    """Test that read_section() finds conversations by filename even when corpus_root uses full paths.

    This is a regression test for the ID lookup bug where conversations stored with
    full paths (e.g., 'conversations:design_discussion') couldn't be looked up by
    filename stem (e.g., 'design_discussion').
    """
    import shutil

    # Copy conversations to a subdirectory under corpus_root
    conversations_dir = temp_corpus / "conversations"
    conversations_dir.mkdir()

    for session_file in temp_sessions.glob("*.json"):
        shutil.copy(session_file, conversations_dir / session_file.name)

    # Create adapter with corpus_root (uses full paths as keys)
    adapter = ClaudeConversationsAdapter(corpus_root=str(temp_corpus))

    adapter._ensure_loaded()
    # Verify conversations are stored with full path keys
    assert "conversations:design_discussion" in adapter._conversations
    assert "conversations:error_handling" in adapter._conversations

    # BUG FIX: read_section() should find by filename STEM even with full path keys
    obs = adapter.read_section("design_discussion")
    assert obs.kind == "read", f"Expected kind='read' but got '{obs.kind}': {obs.error if hasattr(obs, 'error') else ''}"
    assert "pattern" in obs.text.lower()

    # Also verify lookup map was populated
    assert adapter._conv_id_lookup["design_discussion"] == "conversations:design_discussion"
    assert adapter._conv_id_lookup["error_handling"] == "conversations:error_handling"


def test_conversation_query_with_filepaths(temp_sessions):
    """Test that query() returns conversations with FILEPATH markers."""
    adapter = ClaudeConversationsAdapter(sessions_dir=str(temp_sessions))

    obs = adapter.query({"max_clusters": 2, "samples_per_cluster": 1})

    assert obs.kind == "query"
    assert "FILEPATH:" in obs.text  # Should contain filepath markers
    assert os.path.exists(str(temp_sessions))  # Base path should exist

    # Output should contain conversation content
    assert "Conversation:" in obs.text or "USER:" in obs.text.upper()


def test_conversation_query_empty_no_conversations():
    """Test that query() handles the case of no conversations gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = ClaudeConversationsAdapter(sessions_dir=tmpdir)

        obs = adapter.query({})
        assert obs.kind == "error"
        assert "no conversations" in obs.error.lower()
