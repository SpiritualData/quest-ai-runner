#!/usr/bin/env python3
"""Quick test: context card updater with mocked deep runner result."""

import json
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Mock classes
@dataclass
class MockDeepResult:
    """Mock DeepResult with edited_files metadata."""
    met: bool = True
    data: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}


class MockModelProvider:
    """Mock provider that returns categorization."""
    def generate(self, prompt: str, model: str, max_tokens: int, temperature: float) -> str:
        # Simple mock: categorize based on file path patterns
        return """
src/habits/handler.py -> habits-tracking
src/api/planning.py -> daily-actions
tests/test_habits.py -> habits-tracking
docs/habits.md -> habits-tracking
        """


class MockRegistry:
    """Mock registry that returns a tier."""
    def resolve_tier(self, tier: str) -> str:
        return f"mock-{tier}-model"


def test_context_card_categorization():
    """Test that edited files are categorized into context cards."""

    # Set up test data
    deep_result = MockDeepResult(
        met=True,
        data={
            "edited_files": [
                "src/habits/handler.py",
                "src/api/planning.py",
                "tests/test_habits.py",
                "docs/habits.md",
            ]
        }
    )

    context_cards = [
        {
            "id": "habits-tracking",
            "title": "Habit logging and filtering",
            "summary": "Code for backdated habits, daily filtering",
            "files": [
                {"path": "src/habits/handler.py", "why": "main handler"},
                {"path": "tests/test_habits.py", "why": "existing tests"},
            ]
        },
        {
            "id": "daily-actions",
            "title": "Actions for today",
            "summary": "Time-based filtering for daily actions",
            "files": [
                {"path": "src/api/planning.py", "why": "existing"},
            ]
        }
    ]

    context_meta = {
        "cards": context_cards,
        "card_store_dir": tempfile.gettempdir(),
    }

    # Import the updater
    from quest_ai_runner.adapters.context_card_updater import categorize_files_with_llm

    # Test categorization
    provider = MockModelProvider()
    registry = MockRegistry()

    edited_files = deep_result.data["edited_files"]
    print(f"Testing with edited files: {edited_files}\n")

    categorization = categorize_files_with_llm(
        edited_files,
        context_cards,
        model_provider=provider,
        registry=registry,
    )

    print("Categorization result:")
    for card_id, files in categorization.items():
        print(f"  {card_id}:")
        for f in files:
            print(f"    - {f}")

    # Verify results
    assert "habits-tracking" in categorization
    assert "src/habits/handler.py" in categorization["habits-tracking"]
    assert "tests/test_habits.py" in categorization["habits-tracking"]

    assert "daily-actions" in categorization
    assert "src/api/planning.py" in categorization["daily-actions"]

    print("\n✓ All assertions passed!")
    print("\nSummary:")
    print(f"  - Deep runner returned {len(edited_files)} edited files")
    print(f"  - LLM categorized them into {len(categorization)} context cards")
    print(f"  - No new files to add (docs/habits.md not mapped to any card)")


def test_context_card_update_with_new_files():
    """Test that new files are added to cards."""
    from quest_ai_runner.adapters.context_card_updater import update_context_cards

    # Create temporary card files
    with tempfile.TemporaryDirectory() as tmpdir:
        card_store = Path(tmpdir)

        # Create initial card
        card_id = "test-card"
        card_file = card_store / f"{card_id}.json"
        card_data = {
            "id": card_id,
            "title": "Test Card",
            "files": [
                {"path": "existing/file.py", "why": "was here"}
            ]
        }
        card_file.write_text(json.dumps(card_data, indent=2))

        print(f"\nBefore update:")
        print(f"  Card files: {[f['path'] for f in card_data['files']]}")

        # Simulate categorization: new files to add
        categorization = {
            card_id: [
                "new/file1.py",
                "new/file2.py",
            ]
        }

        card_metadata = [card_data]

        # Update cards
        update_context_cards(card_metadata, categorization, str(card_store))

        # Read back and verify
        updated_card = json.loads(card_file.read_text())
        updated_files = [f["path"] for f in updated_card["files"]]

        print(f"\nAfter update:")
        print(f"  Card files: {updated_files}")

        assert "existing/file.py" in updated_files
        assert "new/file1.py" in updated_files
        assert "new/file2.py" in updated_files

        # Verify idempotence: run again with same files
        update_context_cards(card_metadata, categorization, str(card_store))
        updated_card_2 = json.loads(card_file.read_text())
        updated_files_2 = [f["path"] for f in updated_card_2["files"]]

        # Should not have duplicates
        assert len(updated_files_2) == len(updated_files)
        print(f"\n✓ Idempotent: no duplicates after second update")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: File Categorization with LLM")
    print("=" * 60)
    test_context_card_categorization()

    print("\n" + "=" * 60)
    print("TEST 2: Context Card File Updates")
    print("=" * 60)
    test_context_card_update_with_new_files()

    print("\n" + "=" * 60)
    print("✓ All tests passed!")
    print("=" * 60)
