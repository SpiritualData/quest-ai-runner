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
from .tfdfidf_sampling import extract_terms, select_representatives


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
        self._conversation_filepaths: Dict[str, Path] = {}  # Track filepath for each conversation
        self._conv_id_lookup: Dict[str, str] = {}  # Map from filename stem to unique key (for faster lookup)
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

                        # Generate a unique key for this conversation, but allow lookup by filename stem.
                        # For corpus_root, use path prefix to disambiguate same-name files.
                        # For sessions_dir, use just the filename.
                        file_stem = session_file.stem
                        if self.corpus_root:
                            try:
                                rel_path = session_file.relative_to(self.corpus_root)
                                # Use path:filename as unique key to handle collisions
                                unique_key = str(rel_path.with_suffix("")).replace("/", ":")
                            except ValueError:
                                unique_key = file_stem
                        else:
                            unique_key = file_stem

                        self._conversations[unique_key] = data
                        self._conversation_filepaths[unique_key] = session_file.resolve()

                        # Add lookup from filename stem to unique key
                        # If collision occurs, keep the most recent one (last loaded wins)
                        self._conv_id_lookup[file_stem] = unique_key
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

    def _get_conversation_digest(self, conv: Any) -> str:
        """Extract a lightweight digest for embedding.

        Digest includes:
        - First message (sets the topic)
        - Last 2-3 messages (provides closure/summary)
        - Metadata (rep, model, message count)

        This is much cheaper than embedding all messages.
        """
        parts = []
        messages = conv.get("messages") or conv.get("turns") or []

        # Add metadata context
        metadata = self._get_conversation_metadata(conv)
        if metadata:
            meta_str = " | ".join(f"{k}={v}" for k, v in metadata.items())
            parts.append(f"[{meta_str}]")

        # Add first message (usually the user's query)
        if messages:
            first_msg = messages[0]
            if isinstance(first_msg, dict):
                text = first_msg.get("text") or first_msg.get("content") or ""
                if text:
                    parts.append(f"START: {text[:200]}")  # First 200 chars

        # Add last 2-3 messages (outcome/resolution)
        if len(messages) > 1:
            sample_count = min(3, max(1, len(messages) // 2))  # 1-3 messages from the end
            for msg in messages[-sample_count:]:
                if isinstance(msg, dict):
                    text = msg.get("text") or msg.get("content") or ""
                    if text:
                        parts.append(f"RECENT: {text[:150]}")  # Last 150 chars each

        return " ".join(parts) if parts else "empty conversation"

    def _get_conversation_timestamp(self, conv: Any) -> float:
        """Extract timestamp from conversation for recency sorting.

        Returns unix timestamp, or 0 if not available.
        """
        if isinstance(conv, dict):
            # Try common timestamp fields
            for field in ("updated_at", "updatedAt", "createdAt", "created_at", "timestamp"):
                if field in conv:
                    val = conv[field]
                    if isinstance(val, (int, float)):
                        return float(val)
        return 0.0

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

        rel_path is expected to be a conversation id (session name, or filename stem).
        Looks up the conversation using the stored mapping.
        """
        # Extract the conversation id from the path (just the filename, no directory)
        conv_id = rel_path.split("/")[-1]

        # Try direct lookup first (if the exact key exists in _conversations)
        unique_key = None
        if conv_id in self._conversations:
            unique_key = conv_id
        # Then try the lookup map (filename stem → unique key)
        elif conv_id in self._conv_id_lookup:
            unique_key = self._conv_id_lookup[conv_id]
        else:
            return Observation(kind="error", error=f"conversation not found: {conv_id}")

        conv_text = self._conversation_to_text(self._conversations[unique_key])

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

    def _select_representative_conversations(
        self, conv_ids: List[str], samples_per_cluster: int = 2
    ) -> List[str]:
        """Select representative conversations using shared TF-DF-IDF heuristic.

        Uses the shared select_representatives() function, extracting terms from
        digests and weighting by recency.
        """
        if not conv_ids:
            return []

        # Create helper functions that close over self
        def get_digest_terms(cid: str) -> set:
            digest = self._get_conversation_digest(self._conversations[cid])
            return extract_terms(digest)

        def get_recency_boost(cid: str) -> float:
            timestamp = self._get_conversation_timestamp(self._conversations[cid])
            # Small boost for recent conversations
            return 1.0 + (timestamp / 1e11) if timestamp > 0 else 1.0

        return select_representatives(
            conv_ids,
            get_terms=get_digest_terms,
            samples_per_group=samples_per_cluster,
            get_score_boost=get_recency_boost,
        )

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Smart conversation retrieval via TF-DF-IDF sampling.

        Strategy (new, more efficient):
        1. Extract lightweight digests from each conversation (first + last messages)
        2. Use TF-DF-IDF heuristic to identify distinctive conversations
        3. Weight by recency (newer conversations score higher)
        4. Return top-K sampled conversations with filepaths for full retrieval

        Fallback to clustering if available (via _cluster_and_sample).

        Spec keys:
        - "max_clusters": number of topic clusters (default 5) [fallback only]
        - "samples_per_cluster": conversations per cluster to return (default 2)
        - "query": optional query text for reranking (not used yet, reserved)
        - "use_tfidf": use TF-DF-IDF sampling instead of clustering (default True)
        """
        if not self._conversations:
            return Observation(kind="error", error="no conversations available")

        conv_ids = list(self._conversations.keys())
        max_clusters = spec.get("max_clusters", 5)
        samples_per_cluster = spec.get("samples_per_cluster", 2)
        use_tfidf = spec.get("use_tfidf", True)

        # Use TF-DF-IDF sampling by default (faster, more efficient)
        if use_tfidf:
            sampled_ids = self._select_representative_conversations(conv_ids, samples_per_cluster)
        else:
            # Fallback to clustering (if _cluster_and_sample is available)
            digests = {cid: self._get_conversation_digest(self._conversations[cid]) for cid in conv_ids}
            timestamps = {
                cid: self._get_conversation_timestamp(self._conversations[cid]) for cid in conv_ids
            }
            sampled_ids = self._cluster_and_sample(
                conv_ids, digests, timestamps, max_clusters, samples_per_cluster
            )

        if not sampled_ids:
            return Observation(kind="error", error="no conversations selected after sampling")

        # Build response with filepaths for content engine access
        text_parts = []
        for conv_id in sampled_ids:
            filepath = self._conversation_filepaths.get(conv_id, Path("unknown"))
            conv_text = self._conversation_to_text(self._conversations[conv_id])
            text_parts.append(f"=== Conversation: {conv_id} ===\nFILEPATH: {filepath}\n{conv_text}")

        return Observation(
            kind="query",
            text="\n\n".join(text_parts),
        )

    def _cluster_and_sample(
        self,
        conv_ids: List[str],
        digests: Dict[str, str],
        timestamps: Dict[str, float],
        max_clusters: int,
        samples_per_cluster: int,
    ) -> List[str]:
        """Cluster conversations by digest embedding and sample recent from each.

        Falls back to simple recency sampling if clustering unavailable.
        """
        if len(conv_ids) <= samples_per_cluster * 2:
            # Small set: just sort by recency and return top N
            return sorted(conv_ids, key=lambda c: timestamps.get(c, 0), reverse=True)[
                : samples_per_cluster * min(max_clusters, 3)
            ]

        try:
            # Try to use sklearn KMeans for clustering
            from sklearn.cluster import KMeans
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:
            # Fallback: sample by recency only
            return sorted(conv_ids, key=lambda c: timestamps.get(c, 0), reverse=True)[
                : samples_per_cluster * min(max_clusters, 3)
            ]

        try:
            # Vectorize digests using TF-IDF (lightweight, no embedder needed)
            vectorizer = TfidfVectorizer(max_features=100, stop_words="english")
            digest_list = [digests[cid] for cid in conv_ids]
            vectors = vectorizer.fit_transform(digest_list).toarray()

            # Cluster into K topics
            n_clusters = min(max_clusters, len(conv_ids) // 2 or 1)
            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = kmeans.fit_predict(vectors)

            # Group conversations by cluster
            clusters = {}
            for idx, cid in enumerate(conv_ids):
                cluster_id = labels[idx]
                if cluster_id not in clusters:
                    clusters[cluster_id] = []
                clusters[cluster_id].append((timestamps.get(cid, 0), cid))

            # Sample top N most recent from each cluster
            sampled = []
            for cluster_id in sorted(clusters.keys()):
                cluster_convs = clusters[cluster_id]
                # Sort by timestamp (descending, most recent first)
                cluster_convs.sort(reverse=True)
                # Take top samples_per_cluster
                sampled.extend([cid for _, cid in cluster_convs[:samples_per_cluster]])

            return sampled if sampled else [conv_ids[0]]

        except Exception:
            # Fallback to recency sampling
            return sorted(conv_ids, key=lambda c: timestamps.get(c, 0), reverse=True)[
                : samples_per_cluster * min(max_clusters, 3)
            ]

    def list_sources(self) -> Observation:
        """List available conversations.

        Returns all loaded conversations. Use this sparingly — it exposes all loaded
        conversations, including previous/unrelated ones. Prefer explicit read_section()
        by specific conversation ID when possible.
        """
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
