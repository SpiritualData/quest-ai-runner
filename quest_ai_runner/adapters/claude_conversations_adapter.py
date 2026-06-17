"""Claude Code conversations adapter — retrieve context from Claude transcripts.

This adapter reads conversation transcripts from Claude Code's session directory
and makes them available as context for the orchestrator brain.

Example:
    >>> from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter, ClaudeConversationsAdapter
    >>> retrieval = CompositeRetrievalAdapter([
    ...     FilesAdapter("/path/to/corpus"),
    ...     ClaudeConversationsAdapter(sessions_dir="~/.claude/sessions"),
    ... ])
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import Observation, RetrievalAdapter


class ClaudeConversationsAdapter(RetrievalAdapter):
    """RetrievalAdapter over Claude Code session transcripts.

    Reads .json session files (one per conversation) from a configured directory
    and supports read_section (by conversation id) and grep (across all conversations).

    Can initialize from:
    - A corpus root (looks for corpus_root/conversations/)
    - An explicit sessions directory
    - Defaults to ~/.claude/sessions
    """

    def __init__(self, corpus_root: Optional[str] = None, sessions_dir: Optional[str] = None):
        """Initialize with either a corpus root or explicit sessions directory.

        Args:
            corpus_root: Path to corpus root. Recursively scans for:
                        - .claude/ directories within the corpus
                        - conversations/ subdirectories
                        - *.json conversation files at any depth
                        Takes precedence over sessions_dir.
            sessions_dir: Path to Claude Code sessions directory explicitly.
                         Used if corpus_root is not provided.
                         Defaults to ~/.claude/sessions if both are None.
        """
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self.sessions_dir = Path(sessions_dir) if sessions_dir else Path.home() / ".claude" / "sessions"
        self._conversations: Dict[str, Any] = {}
        self._load_conversations()

    def _load_conversations(self) -> None:
        """Load all .json conversation files recursively.

        If corpus_root is set, recursively scans for:
        - .claude/ directories
        - conversations/ subdirectories
        - *.json files at any depth

        Otherwise uses the explicit sessions_dir.
        """
        search_dirs = []

        # If corpus_root is set, scan it recursively for conversation sources
        if self.corpus_root and self.corpus_root.is_dir():
            try:
                # Find .claude directories anywhere in the corpus
                search_dirs.extend(self.corpus_root.glob("**/.claude"))
                # Find conversations/ directories anywhere in the corpus
                search_dirs.extend(self.corpus_root.glob("**/conversations"))
            except Exception:  # noqa: BLE001
                pass

        # Also add explicit sessions_dir if it exists
        if self.sessions_dir.is_dir():
            search_dirs.append(self.sessions_dir)

        if not search_dirs:
            return  # No directories to search

        # Remove duplicates while preserving order
        seen = set()
        unique_dirs = []
        for d in search_dirs:
            d_abs = d.resolve()
            if d_abs not in seen:
                seen.add(d_abs)
                unique_dirs.append(d)

        # Load all .json files from all search directories
        for search_dir in unique_dirs:
            try:
                for session_file in search_dir.glob("*.json"):
                    try:
                        with open(session_file) as f:
                            data = json.load(f)

                        # Check if this looks like a Claude conversation
                        # (has messages/turns field with role/text structure)
                        if not self._is_claude_conversation(data):
                            continue  # Skip non-conversation JSON files

                        # Use relative path from corpus_root as session_id (for uniqueness)
                        if self.corpus_root:
                            try:
                                rel_path = session_file.relative_to(self.corpus_root)
                                session_id = str(rel_path.with_suffix("")).replace("/", ":")
                            except ValueError:
                                session_id = session_file.stem
                        else:
                            session_id = session_file.stem
                        self._conversations[session_id] = data
                    except (json.JSONDecodeError, OSError):
                        pass  # Skip unreadable files
            except Exception:  # noqa: BLE001
                pass  # Silently degrade if directory scan fails

    @staticmethod
    def _is_claude_conversation(data: Any) -> bool:
        """Check if a JSON object looks like a Claude conversation.

        A valid Claude conversation has:
        - A messages or turns array
        - Message objects with role (user/assistant) and text/content
        """
        if not isinstance(data, dict):
            return False

        messages = data.get("messages") or data.get("turns")
        if not isinstance(messages, list) or not messages:
            return False

        # Check if messages have the right structure (role + text/content)
        for msg in messages:
            if not isinstance(msg, dict):
                return False
            has_role = "role" in msg
            has_text = "text" in msg or "content" in msg
            if not (has_role and has_text):
                return False

        return True

    def _get_conversation_metadata(self, conv: Any) -> Dict[str, Any]:
        """Extract metadata from a Claude conversation dict.

        Returns fields like rep_name, turn_count, model used, etc.
        """
        metadata = {}
        if isinstance(conv, dict):
            # Extract Claude-specific metadata
            if "rep_name" in conv:
                metadata["rep"] = conv["rep_name"]
            if "turn_count" in conv:
                metadata["turns"] = conv["turn_count"]
            if "model_hint" in conv:
                metadata["model"] = conv["model_hint"]
            # Count actual messages if available
            messages = conv.get("messages") or conv.get("turns") or []
            metadata["message_count"] = len(messages)
        return metadata

    def _conversation_to_text(self, conv: Any) -> str:
        """Convert a conversation dict to readable text with structure preserved.

        Handles Claude conversation format with role/text pairs and preserves
        metadata like rep_name and model used.
        """
        parts = []
        if isinstance(conv, dict):
            # Extract and display metadata header
            metadata = self._get_conversation_metadata(conv)
            if metadata:
                meta_str = " | ".join(f"{k}={v}" for k, v in metadata.items())
                parts.append(f"[{meta_str}]")
                parts.append("")

            # Render message turns
            messages = conv.get("messages") or conv.get("turns") or []
            for msg in messages:
                if isinstance(msg, dict):
                    role = msg.get("role", "unknown")
                    text = msg.get("text") or msg.get("content") or ""
                    # Format: "role: text" with role in caps for clarity
                    parts.append(f"{role.upper()}: {text}")
                else:
                    parts.append(str(msg))

        return "\n".join(parts)

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Read a conversation by id or path.

        rel_path is expected to be a conversation id (session name).
        """
        conv_id = rel_path.split("/")[-1]  # e.g., "my_session" from "conversations/my_session"
        if conv_id not in self._conversations:
            return Observation(kind="error", error=f"conversation not found: {conv_id}")

        conv_text = self._conversation_to_text(self._conversations[conv_id])

        # Apply line range if specified
        if start_line or end_line:
            lines = conv_text.split("\n")
            start = (start_line or 1) - 1
            end = end_line or len(lines)
            conv_text = "\n".join(lines[max(0, start) : min(len(lines), end)])

        # Apply byte limit if specified
        if max_bytes and len(conv_text) > max_bytes:
            conv_text = conv_text[:max_bytes].rsplit("\n", 1)[0] + "\n[truncated]"

        return Observation(
            kind="read",
            rel_path=rel_path,
            text=conv_text,
        )

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Search for a pattern across all conversations."""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return Observation(kind="error", pattern=pattern, error=f"invalid regex: {e}")

        hits: List[Dict[str, Any]] = []
        for conv_id, conv_data in self._conversations.items():
            conv_text = self._conversation_to_text(conv_data)
            for i, line in enumerate(conv_text.split("\n"), 1):
                if regex.search(line):
                    hits.append({
                        "line": line,
                        "line_number": i,
                        "file": conv_id,
                    })
                    if max_hits and len(hits) >= max_hits:
                        break
            if max_hits and len(hits) >= max_hits:
                break

        if not hits:
            return Observation(kind="error", pattern=pattern, error=f"pattern not found: {pattern}")

        return Observation(kind="grep", pattern=pattern, hits=hits)

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Simple semantic search stub. For now, returns all conversations."""
        # TODO: implement semantic search using embeddings
        text_parts = []
        for conv_id, conv_data in self._conversations.items():
            conv_text = self._conversation_to_text(conv_data)
            text_parts.append(f"=== Conversation: {conv_id} ===\n{conv_text}")

        if not text_parts:
            return Observation(kind="error", error="no conversations available")

        return Observation(
            kind="query",
            text="\n\n".join(text_parts),
        )

    def list_sources(self) -> Observation:
        """List available conversations."""
        if not self._conversations:
            return Observation(kind="error", error="no conversations loaded")

        sources = "\n".join([f"{cid}: Claude conversation" for cid in sorted(self._conversations.keys())])
        return Observation(kind="query", text=sources)

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """Describe a conversation by id."""
        if name not in self._conversations:
            return Observation(kind="error", error=f"conversation not found: {name}")

        conv = self._conversations[name]
        msg_count = len(conv.get("messages") or conv.get("turns") or [])
        return Observation(
            kind="query",
            text=f"Conversation '{name}' with {msg_count} messages.",
        )

    def list_operations(self) -> Observation:
        """List operations (none for read-only conversations)."""
        return Observation(kind="query", text="read: read conversation transcript by id\ngrep: search for patterns across conversations")

    def describe_operation(self, name: str) -> Observation:
        """Describe an operation."""
        if name == "read":
            return Observation(kind="query", text="read(conversation_id) -> transcript text")
        elif name == "grep":
            return Observation(kind="query", text="grep(pattern) -> matching lines from all conversations")
        return Observation(kind="error", error=f"operation not found: {name}")
