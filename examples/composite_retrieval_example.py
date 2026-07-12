#!/usr/bin/env python3
"""Example: multi-source retrieval with Claude conversations in corpus.

This example shows how to:
1. Organize Claude conversations within your corpus directory
2. Wire multiple retrieval adapters (files + conversations + optional database)
3. Query them all in parallel when the orchestrator needs context
4. See how Claude conversations augment grounding on files

Run:
    python3 composite_retrieval_example.py
"""
import json
import tempfile
from pathlib import Path

from quest_ai_runner.adapters import (
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
    FilesAdapter,
)
from quest_ai_runner.config import RunnerConfig, build_orchestrator


def setup_example_corpus():
    """Create an example corpus with files and conversations."""
    tmpdir = Path(tempfile.mkdtemp())

    # Create docs
    (tmpdir / "docs").mkdir()
    (tmpdir / "docs" / "architecture.md").write_text("""
# Architecture

Our system uses a pattern-based design with three layers:
1. Retrieval layer - adapters query the source
2. Orchestration layer - plans, gathers, re-plans
3. Model layer - LLM calls for reasoning

Key design decision: no lossy summarization. The brain sees full context from all sources.
    """)

    (tmpdir / "docs" / "api.md").write_text("""
# API Contract

The orchestrator implements the following pattern:
- plan(): structured decision on what to read/compute
- gather(): read from adapters (files, DB, conversations, etc.)
- replan(): refine based on what was gathered
- answer(): generate final response
    """)

    # Create a conversations subdirectory
    (tmpdir / "conversations").mkdir()

    # Add example conversations
    design_conv = {
        "rep_name": "Alex's AI",
        "turn_count": 3,
        "messages": [
            {
                "role": "user",
                "text": "What design patterns should we use for the retrieval layer?",
            },
            {
                "role": "assistant",
                "text": "The retrieval layer should use adapters to query multiple sources in parallel. "
                "Files, databases, and conversations all implement the RetrievalAdapter interface.",
            },
            {"role": "user", "text": "How do we avoid information loss?"},
            {
                "role": "assistant",
                "text": "By not summarizing across sources. The orchestrator sees full context "
                "from each adapter. This follows Shannon's Data Processing Inequality.",
            },
        ],
    }

    implementation_conv = {
        "rep_name": "Alex's AI",
        "turn_count": 2,
        "messages": [
            {"role": "user", "text": "How do we implement the grep method for conversations?"},
            {
                "role": "assistant",
                "text": "Use regex to search conversation text across all loaded sessions. "
                "Deduplicate hits by line + source adapter to avoid duplicates.",
            },
        ],
    }

    (tmpdir / "conversations" / "design_decisions.json").write_text(json.dumps(design_conv))
    (tmpdir / "conversations" / "implementation.json").write_text(json.dumps(implementation_conv))

    print(f"✓ Example corpus created at: {tmpdir}")
    print(f"  - {tmpdir}/docs/ (architecture.md, api.md)")
    print(f"  - {tmpdir}/conversations/ (design_decisions.json, implementation.json)")

    return tmpdir


def main():
    print("Multi-Source Retrieval Example\n")

    # Set up example corpus with files and conversations
    corpus_root = setup_example_corpus()

    print("\n--- Setup ---\n")

    # Create adapters
    files_adapter = FilesAdapter(str(corpus_root))
    conversations_adapter = ClaudeConversationsAdapter(corpus_root=str(corpus_root))

    print("✓ FilesAdapter: reads from docs/")
    print("✓ ClaudeConversationsAdapter: reads from conversations/")

    # Wire them together with CompositeRetrievalAdapter
    retrieval = CompositeRetrievalAdapter(
        [files_adapter, conversations_adapter],
        max_workers=2,
    )

    print("✓ CompositeRetrievalAdapter: queries both in parallel")

    # Create orchestrator with composite retrieval
    cfg = RunnerConfig(
        retrieval=retrieval,
        # Note: in a real setup, you'd wire a model_provider here
        # For this example, we're just showing retrieval setup
    )

    print("\n--- Discovering Sources ---\n")

    # Show what sources are available
    sources = retrieval.list_sources()
    print("Available sources:")
    print(sources.text)

    print("\n--- Searching Across All Sources ---\n")

    # Search for a pattern across both files and conversations
    pattern = "adapter"
    print(f'Searching for "{pattern}" across docs and conversations...\n')

    results = retrieval.grep(pattern)
    print(f"Found {len(results.hits)} hits:")
    for i, hit in enumerate(results.hits[:5], 1):
        print(f"  {i}. {hit.get('file', '?')}: {hit.get('line', '')[:60]}")

    print("\n--- Reading from Conversations ---\n")

    # Read a specific conversation
    conv = retrieval.read_section("design_decisions")
    print("=== design_decisions conversation ===")
    print(conv.text)

    print("\n--- Reading from Files ---\n")

    # Read a specific file
    arch = retrieval.read_section("docs/architecture.md")
    print("=== docs/architecture.md ===")
    print(arch.text[:200] + "...")

    print("\n✓ Multi-source retrieval working!\n")
    print(f"Corpus location: {corpus_root}")
    print("To use this in a real orchestrator:")
    print("  1. Wire a ModelProvider (e.g., AnthropicProvider)")
    print("  2. Pass the retrieval + model to build_orchestrator()")
    print("  3. Call orch.run(user_question)")
    print("\nThe orchestrator will now ground on files AND conversations automatically.")


if __name__ == "__main__":
    main()
