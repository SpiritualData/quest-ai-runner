"""Context card updater: categorize edited files into context cards.

After deep execution, this module:
1. Receives edited files from the deep runner's result metadata
2. Uses a fast LLM call to categorize them into relevant context cards
3. Updates the cards to include new files (idempotent — only adds if not already present)

Note: File discovery happens in the deep runner itself (Claude Code / orchestrator), which
returns edited file metadata as structured data. This module only categorizes and updates.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


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
