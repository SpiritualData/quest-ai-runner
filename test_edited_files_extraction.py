#!/usr/bin/env python3
"""Test: discover edited files from Claude Code sessions and other deep runners."""

import json
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Mock result classes to simulate deep runners
@dataclass
class MockDeepResult:
    """Mock result with optional structured data."""
    met: bool = True
    output: str = ""
    data: Optional[Dict[str, Any]] = None


def test_extract_edited_files_from_output_yaml():
    """Test parsing YAML-style EDITED_FILES output."""
    from quest_ai_runner.adapters.context_card_updater import _parse_edited_files_from_output

    output = """\
Task completed successfully.

EDITED_FILES:
- src/habits/handler.py
- src/api/planning.py
- tests/test_habits.py

Next steps: deploy
"""

    files = _parse_edited_files_from_output(output)
    assert files == ["src/habits/handler.py", "src/api/planning.py", "tests/test_habits.py"]
    print("✓ YAML-style parsing works")


def test_extract_edited_files_from_output_json():
    """Test parsing JSON-style edited_files output."""
    from quest_ai_runner.adapters.context_card_updater import _parse_edited_files_from_output

    output = json.dumps({
        "status": "completed",
        "message": "Refactoring complete",
        "edited_files": ["src/api/core.py", "tests/api_test.py"]
    })

    files = _parse_edited_files_from_output(output)
    assert files == ["src/api/core.py", "tests/api_test.py"]
    print("✓ JSON parsing works")


def test_extract_edited_files_from_result_metadata():
    """Test extracting edited_files from result.data["edited_files"]."""
    from quest_ai_runner.adapters.context_card_updater import extract_edited_files

    result = MockDeepResult(
        met=True,
        data={
            "edited_files": ["src/main.py", "docs/README.md"],
            "status": "completed"
        }
    )

    files = extract_edited_files(result)
    assert files == ["src/main.py", "docs/README.md"]
    print("✓ Result metadata extraction works")


def test_extract_edited_files_fallback_to_output():
    """Test fallback to output parsing when metadata is missing."""
    from quest_ai_runner.adapters.context_card_updater import extract_edited_files

    result = MockDeepResult(
        met=True,
        output="""\
EDITED_FILES:
- src/handler.py
- tests/handler_test.py
"""
    )

    files = extract_edited_files(result)
    assert files == ["src/handler.py", "tests/handler_test.py"]
    print("✓ Fallback to output parsing works")


def test_claude_code_jsonl_parsing():
    """Test parsing Claude Code session JSONL."""
    from quest_ai_runner.adapters.context_card_updater import discover_claude_code_edits

    # Create a temporary session structure
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        project_name = project_path.name
        session_id = "test-session-123"

        # Create the session directory and JSONL file
        session_dir = project_path / ".claude" / "projects" / project_name / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Create mock session JSONL with Edit tool calls
        messages_jsonl = session_dir / "messages.jsonl"
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit-1",
                        "name": "edit",
                        "input": {
                            "file_path": "src/api/routes.py",
                            "old_string": "def handler(): pass",
                            "new_string": "def handler(): return 'ok'"
                        }
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "edit-1",
                        "content": json.dumps({"file_path": "src/api/routes.py", "result": "ok"})
                    }
                ]
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit-2",
                        "name": "edit",
                        "input": {
                            "file_path": "tests/test_routes.py",
                            "old_string": "# TODO: test",
                            "new_string": "def test_handler(): assert handler() == 'ok'"
                        }
                    }
                ]
            }
        ]

        # Write JSONL (one JSON object per line)
        with open(messages_jsonl, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

        # Test discovery
        files = discover_claude_code_edits(str(project_path), session_id)
        assert "src/api/routes.py" in files
        assert "tests/test_routes.py" in files
        print(f"✓ Claude Code JSONL parsing works: found {len(files)} files")


def test_get_prompt_instruction():
    """Test that prompt instruction is well-formed."""
    from quest_ai_runner.adapters.context_card_updater import get_edited_files_prompt_instruction

    instruction = get_edited_files_prompt_instruction()
    assert "EDITED_FILES:" in instruction
    assert "edited_files" in instruction
    assert len(instruction) > 100
    print(f"✓ Prompt instruction generation works ({len(instruction)} chars)")


if __name__ == "__main__":
    print("Testing edited files extraction strategies...\n")
    test_extract_edited_files_from_output_yaml()
    test_extract_edited_files_from_output_json()
    test_extract_edited_files_from_result_metadata()
    test_extract_edited_files_fallback_to_output()
    test_claude_code_jsonl_parsing()
    test_get_prompt_instruction()
    print("\n✅ All tests passed!")
