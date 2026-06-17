"""Guidance card manager — load, track, and auto-sync guidance cards across environments.

Guidance cards are behavior/interface instructions that the orchestrator injects before
planning, so the brain operates under consistent, environment-specific policies.

This manager:
- Loads cards from a configurable directory
- Detects changes (adds, edits, deletes) via file timestamps and fingerprinting
- Auto-syncs changes to any configured backend (file-based, vector store, database)
- Provides a stable, versioned interface for the ContextAssembler

Cards are simple markdown files with optional YAML frontmatter:

    ---
    id: my_guidance_card
    description: What this card is about
    ---
    # Card Title
    Card content in markdown...

Example:
    >>> manager = GuidanceCardManager(cards_dir="/path/to/guidance")
    >>> cards = manager.load_cards()  # Auto-detects changes
    >>> for card in cards:
    ...     print(card["id"], card["body"])
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class GuidanceCard:
    """A single guidance card with metadata and content."""
    id: str
    title: str
    body: str
    description: str = ""
    tags: List[str] = None
    fingerprint: str = ""  # Hash for change detection
    file_path: str = ""
    modified_at: float = 0.0

    def __post_init__(self):
        if self.tags is None:
            self.tags = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for storage/API."""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "description": self.description,
            "tags": self.tags,
            "fingerprint": self.fingerprint,
            "file_path": self.file_path,
            "modified_at": self.modified_at,
        }


class GuidanceCardManager:
    """Load and track guidance cards from a directory, auto-detecting changes.

    Cards are markdown files with optional YAML frontmatter. Changes are detected
    via file modification times and content fingerprints, enabling auto-sync to
    any backend (files, vector stores, databases).
    """

    def __init__(self, cards_dir: Optional[str] = None):
        """Initialize with a cards directory.

        Args:
            cards_dir: Path to guidance card directory. If None, defaults to
                      cwd/.quest-guidance or /app/prompts/guidance (Quest backend).
        """
        self.cards_dir = self._resolve_cards_dir(cards_dir)
        self._loaded_cards: Dict[str, GuidanceCard] = {}
        self._fingerprints: Dict[str, str] = {}  # Track for change detection

    def _resolve_cards_dir(self, cards_dir: Optional[str]) -> Path:
        """Resolve the cards directory, checking standard locations."""
        if cards_dir:
            return Path(cards_dir)

        # Standard locations (in order of preference)
        candidates = [
            Path.cwd() / ".quest-guidance",
            Path("/app/prompts/guidance"),  # Quest backend default
            Path.home() / ".quest" / "guidance",
        ]

        for candidate in candidates:
            if candidate.is_dir():
                return candidate

        # Default: create/use .quest-guidance in cwd
        return Path.cwd() / ".quest-guidance"

    def load_cards(self, force_reload: bool = False) -> List[GuidanceCard]:
        """Load guidance cards, auto-detecting changes since last load.

        Args:
            force_reload: If True, reload all cards even if not changed.

        Returns:
            List of GuidanceCard objects. Returns cached if unchanged.
        """
        if not self.cards_dir.exists():
            return []

        loaded = {}
        changed = False

        try:
            for card_file in sorted(self.cards_dir.glob("*.md")):
                card = self._load_card_file(card_file)
                if card:
                    loaded[card.id] = card
                    # Check if fingerprint changed
                    old_fp = self._fingerprints.get(card.id, "")
                    if card.fingerprint != old_fp:
                        changed = True
                    self._fingerprints[card.id] = card.fingerprint
        except Exception as e:  # noqa: BLE001
            pass  # Silently degrade; return what we have

        # Detect deletions
        if set(self._loaded_cards.keys()) != set(loaded.keys()):
            changed = True

        self._loaded_cards = loaded
        return list(loaded.values())

    def _load_card_file(self, file_path: Path) -> Optional[GuidanceCard]:
        """Parse a single markdown card file with optional YAML frontmatter."""
        try:
            content = file_path.read_text(encoding="utf-8")
            card_id = file_path.stem

            # Parse frontmatter if present
            frontmatter = {}
            body = content
            if content.startswith("---"):
                try:
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = yaml.safe_load(parts[1]) or {}
                        body = parts[2].lstrip("\n")
                except (yaml.YAMLError, ValueError):
                    pass  # Use content as-is if YAML parsing fails

            # Extract title from body (first heading) if not in frontmatter
            title = frontmatter.get("title", "")
            if not title:
                match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                if match:
                    title = match.group(1).strip()
            if not title:
                title = card_id.replace("_", " ").title()

            # Compute fingerprint for change detection
            fingerprint = hashlib.sha256(content.encode()).hexdigest()

            stat = file_path.stat()
            return GuidanceCard(
                id=frontmatter.get("id", card_id),
                title=title,
                body=body,
                description=frontmatter.get("description", ""),
                tags=frontmatter.get("tags", []),
                fingerprint=fingerprint,
                file_path=str(file_path),
                modified_at=stat.st_mtime,
            )
        except Exception as e:  # noqa: BLE001
            return None

    def get_card(self, card_id: str) -> Optional[GuidanceCard]:
        """Get a single card by ID."""
        return self._loaded_cards.get(card_id)

    def has_changes(self) -> bool:
        """Check if any cards have changed since last load."""
        if not self.cards_dir.exists():
            return False

        for card_file in self.cards_dir.glob("*.md"):
            card = self._load_card_file(card_file)
            if card and self._fingerprints.get(card.id, "") != card.fingerprint:
                return True

        # Check for deletions
        if set(self._loaded_cards.keys()) != set(
            {f.stem for f in self.cards_dir.glob("*.md")}
        ):
            return True

        return False

    def save_card(self, card_id: str, title: str, body: str, **metadata) -> bool:
        """Save/update a guidance card to file.

        Args:
            card_id: Card identifier (used as filename)
            title: Card title
            body: Card markdown body
            **metadata: Additional frontmatter (description, tags, etc.)

        Returns:
            True if saved successfully, False otherwise.
        """
        if not self.cards_dir.exists():
            self.cards_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Build frontmatter
            frontmatter = {"id": card_id, "title": title}
            frontmatter.update(metadata)

            # Write file
            card_file = self.cards_dir / f"{card_id}.md"
            content = f"---\n{yaml.dump(frontmatter)}---\n{body}"
            card_file.write_text(content, encoding="utf-8")

            # Update cache
            card = self._load_card_file(card_file)
            if card:
                self._loaded_cards[card_id] = card
                self._fingerprints[card_id] = card.fingerprint
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete_card(self, card_id: str) -> bool:
        """Delete a guidance card file."""
        try:
            # Find and delete the file
            for card_file in self.cards_dir.glob("*.md"):
                if card_file.stem == card_id:
                    card_file.unlink()
                    self._loaded_cards.pop(card_id, None)
                    self._fingerprints.pop(card_id, None)
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False
