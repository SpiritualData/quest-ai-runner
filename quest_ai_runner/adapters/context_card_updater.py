"""Context card updater: categorize edited files into context cards.

After deep execution, this module:
1. Discovers edited files from the deep runner:
   - Claude Code: parses ~/.claude/projects/<project>/<session_id>/messages.jsonl
   - Other runners: extracts from structured result metadata or parses prompted format
2. Uses a fast LLM call to categorize them into relevant context cards
3. Updates the cards to include new files (idempotent — only adds if not already present)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


def discover_claude_code_edits(project_path: str, session_id: str) -> List[str]:
    """Parse Claude Code session JSONL to extract file paths from Edit tool calls.

    Claude Code stores session transcripts at:
    ~/.claude/projects/<project>/<session_id>/messages.jsonl

    Each message is a JSONL line containing tool calls, including Edit operations.

    Args:
        project_path: Path to project root (where .claude/projects/ lives)
        session_id: Session ID from the deep runner

    Returns:
        List of file paths that were edited in this session
    """
    edited_files = []

    # Construct the session JSONL path
    session_file = Path(project_path) / ".claude" / "projects" / Path(project_path).name / session_id / "messages.jsonl"

    if not session_file.exists():
        log.debug(f"Session file not found: {session_file}")
        return edited_files

    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Look for tool use blocks in the message
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if block.get("type") == "tool_use" and block.get("name") == "edit":
                        # Extract file path from Edit tool input
                        input_data = block.get("input", {})
                        file_path = input_data.get("file_path")
                        if file_path and file_path not in edited_files:
                            edited_files.append(file_path)

                    # Also check for tool_result blocks that may contain file info
                    elif block.get("type") == "tool_result":
                        # Tool results might indicate successful edits
                        # The Edit tool returns {"file_path": "...", "result": "..."}
                        content_str = block.get("content", "")
                        if isinstance(content_str, str) and "file_path" in content_str:
                            try:
                                result_data = json.loads(content_str)
                                if "file_path" in result_data:
                                    fp = result_data["file_path"]
                                    if fp and fp not in edited_files:
                                        edited_files.append(fp)
                            except json.JSONDecodeError:
                                pass

        log.debug(f"Discovered {len(edited_files)} edited files from Claude Code session {session_id}")
    except Exception as e:
        log.error(f"Failed to parse Claude Code session {session_id}: {e}")

    return edited_files


def extract_edited_files(
    deep_result: Any,
    project_path: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[str]:
    """Extract edited files from a deep runner result using multiple strategies.

    Tries in order:
    1. If deep_result has .data["edited_files"], use that (structured metadata)
    2. If deep_result has .output and it looks like JSONL with file format, parse it
    3. If project_path and session_id provided, try Claude Code session JSONL

    Args:
        deep_result: The result from the deep runner
        project_path: Optional path to project (for Claude Code session parsing)
        session_id: Optional session ID (for Claude Code session parsing)

    Returns:
        List of file paths that were edited
    """
    edited_files = []

    # Strategy 1: Check for structured metadata in result
    if hasattr(deep_result, "data") and isinstance(deep_result.data, dict):
        if "edited_files" in deep_result.data:
            ef = deep_result.data["edited_files"]
            if isinstance(ef, list):
                edited_files.extend(ef)
                if edited_files:
                    log.debug(f"Found {len(edited_files)} files from result.data['edited_files']")
                    return edited_files

    # Strategy 2: Try to parse output as structured format
    output_text = ""
    if hasattr(deep_result, "output") and isinstance(deep_result.output, str):
        output_text = deep_result.output

    if output_text:
        edited_files.extend(_parse_edited_files_from_output(output_text))
        if edited_files:
            log.debug(f"Found {len(edited_files)} files from output parsing")
            return edited_files

    # Strategy 3: Try Claude Code session JSONL parsing
    if project_path and session_id:
        edited_files.extend(discover_claude_code_edits(project_path, session_id))
        if edited_files:
            log.debug(f"Found {len(edited_files)} files from Claude Code session")
            return edited_files

    log.debug("No edited files discovered from any strategy")
    return edited_files


def get_edited_files_prompt_instruction() -> str:
    """Get a standard prompt instruction for deep runners to report edited files.

    Include this in the system prompt or final instruction for deep runners
    that aren't Claude Code (e.g., Managed Agents, direct API calls).

    Returns:
        Prompt instruction to include in deep runner context
    """
    return """\
At the end of your work, report which files you edited in this standard format:

EDITED_FILES:
- path/to/file1.py
- path/to/file2.ts
- path/to/file3.md

Include only files you directly modified. Do not include files that were merely read or referenced.
If you edited no files, write:

EDITED_FILES:

Alternatively, if returning JSON output, include an "edited_files" field:

{
  "status": "completed",
  "edited_files": ["path/to/file1.py", "path/to/file2.ts"]
}

This helps the system learn which context cards are relevant to this task."""


def _parse_edited_files_from_output(output_text: str) -> List[str]:
    """Parse edited files from runner output in standard format.

    Expected format (one per line or JSON block):
    EDITED_FILES:
    - path/to/file1.py
    - path/to/file2.ts

    Or:
    {"edited_files": ["path/to/file1.py", "path/to/file2.ts"]}

    Args:
        output_text: The output text from the runner

    Returns:
        List of file paths
    """
    edited_files = []

    # Try JSON format first
    try:
        data = json.loads(output_text)
        if isinstance(data, dict) and "edited_files" in data:
            ef = data["edited_files"]
            if isinstance(ef, list):
                edited_files.extend([f for f in ef if isinstance(f, str)])
                return edited_files
    except json.JSONDecodeError:
        pass

    # Try YAML-style format
    lines = output_text.split("\n")
    in_files_section = False
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("EDITED_FILES:"):
            in_files_section = True
            continue

        if in_files_section:
            if stripped.startswith("- "):
                # YAML list item
                file_path = stripped[2:].strip()
                if file_path:
                    edited_files.append(file_path)
            elif stripped and not stripped.startswith("- ") and not stripped.startswith("#"):
                # End of list (non-list-item, non-comment line)
                break

    return edited_files


def categorize_files_into_cards(
    edited_files: List[str],
    card_metadata: List[Dict[str, Any]],
    categorizer_fn: Callable[[List[str], List[Dict[str, Any]]], Dict[str, List[str]]],
) -> Dict[str, List[str]]:
    """Use an LLM to map edited files to relevant context cards.

    Args:
        edited_files: File paths that were modified (passed from deep runner result)
        card_metadata: The context cards that were used for this task
        categorizer_fn: A callable(file_paths, card_metadata) -> {card_id: [files]}

    Returns:
        Mapping of card_id -> list of file paths to add
    """
    if not edited_files or not card_metadata:
        return {}

    try:
        categorization = categorizer_fn(edited_files, card_metadata)
        return categorization or {}
    except Exception as e:
        log.error(f"File categorization failed: {e}")
        return {}


def update_context_cards(
    card_metadata: List[Dict[str, Any]],
    categorization: Dict[str, List[str]],
    card_store_dir: str,
) -> None:
    """Update context card JSON files to include newly discovered files.

    Only adds files that aren't already in the card. Idempotent.

    Args:
        card_metadata: The card definitions (must have "id" key)
        categorization: {card_id: [file_paths]} from categorizer
        card_store_dir: Directory where card JSON files are stored
    """
    if not categorization:
        return

    for card in card_metadata:
        card_id = card.get("id")
        if not card_id or card_id not in categorization:
            continue

        new_files = categorization[card_id]
        if not new_files:
            continue

        # Try to load and update the card JSON
        card_file = Path(card_store_dir) / f"{card_id}.json"
        if not card_file.exists():
            log.debug(f"Card file not found: {card_file}")
            continue

        try:
            with open(card_file, "r", encoding="utf-8") as f:
                card_data = json.load(f)
        except Exception as e:
            log.error(f"Failed to read card {card_id}: {e}")
            continue

        # Merge new files into existing file list
        existing_files: Set[str] = {f.get("path") for f in card_data.get("files", []) if f.get("path")}
        files_to_add = [fp for fp in new_files if fp not in existing_files]

        if not files_to_add:
            continue

        # Add new files to the card (simple append with minimal metadata)
        for fp in files_to_add:
            card_data["files"].append({
                "path": fp,
                "why": "discovered during deep execution",
            })

        # Write back
        try:
            with open(card_file, "w", encoding="utf-8") as f:
                json.dump(card_data, f, indent=2)
            log.debug(f"Updated card {card_id} with {len(files_to_add)} new files")
        except Exception as e:
            log.error(f"Failed to write card {card_id}: {e}")


def categorize_files_with_llm(
    file_paths: List[str],
    card_metadata: List[Dict[str, Any]],
    model_provider: Any,
    registry: Any,
) -> Dict[str, List[str]]:
    """Use LLM (fast tier) to categorize files into context cards.

    Args:
        file_paths: Files that were modified
        card_metadata: Context cards used for this task
        model_provider: The model provider (e.g., Claude, Gemini)
        registry: ModelRegistry to resolve the fast tier model

    Returns:
        Dict mapping card_id -> list of file paths that belong to that card
    """
    if not file_paths or not card_metadata:
        return {}

    # Resolve fast tier model from registry (not hardcoded)
    fast_model = registry.resolve_tier("fast")

    # Build the card summaries for the LLM
    card_summaries = []
    for card in card_metadata:
        card_id = card.get("id", "?")
        title = card.get("title", "(no title)")
        summary = card.get("summary", "")
        card_summaries.append(f"- {card_id}: {title}\n  {summary}")

    prompt = f"""You have a list of files that were modified during code execution.
Your task is to categorize each file into one of the context cards below based on semantic relevance.

Context cards (the execution was scoped to these topics):
{chr(10).join(card_summaries)}

Modified files:
{chr(10).join(f"- {fp}" for fp in file_paths)}

For each file, respond with ONLY the card_id it belongs in, one per line.
If a file doesn't clearly belong to any card, respond with "NONE".
Format: <file_path> -> <card_id>

Keep responses terse and accurate.
"""

    try:
        result = model_provider.generate(
            prompt,
            model=fast_model,
            max_tokens=500,
            temperature=0.3,
        )

        # Parse response: "path -> card_id" lines
        categorization: Dict[str, List[str]] = {}
        for line in (result or "").split("\n"):
            line = line.strip()
            if "->" not in line:
                continue
            parts = line.split("->")
            if len(parts) != 2:
                continue
            file_path = parts[0].strip().lstrip("- ").strip("'\"")
            card_id = parts[1].strip()

            if card_id != "NONE" and file_path in file_paths:
                if card_id not in categorization:
                    categorization[card_id] = []
                categorization[card_id].append(file_path)

        return categorization
    except Exception as e:
        log.error(f"LLM categorization failed: {e}")
        return {}
