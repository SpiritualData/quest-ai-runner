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

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext, Observation, RetrievalAdapter
from .conversation_format import (
    conversation_digest,
    conversation_metadata,
    conversation_timestamp,
    conversation_to_text,
    is_claude_conversation,
    load_conversations,
    parse_date_bound,
    resolve_conv_key,
    timestamp_in_range,
    truncate_transcript_middle,
)
from .tfdfidf_sampling import extract_terms, keywords_from_text, select_representatives


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
        self._conversation_filepaths: Dict[str, Path] = {}
        self._conv_id_lookup: Dict[str, str] = {}
        self._loaded = False  # lazy: scan deferred until first actual use

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_conversations()

    def _load_conversations(self) -> None:
        """Scan and load all .json conversation files (delegates to the shared loader).

        If corpus_root is set, recursively scans for .claude/ dirs, conversations/ subdirs, and
        *.json files at any depth; otherwise uses the explicit sessions_dir.
        """
        self._conversations, self._conversation_filepaths, self._conv_id_lookup = load_conversations(
            self.corpus_root, self.sessions_dir
        )
        self._loaded = True

    # Thin instance wrappers over the shared, module-level helpers — kept so existing call sites
    # (and any external callers) keep working with identical behavior.
    @staticmethod
    def _is_claude_conversation(data: Any) -> bool:
        return is_claude_conversation(data)

    def _get_conversation_metadata(self, conv: Any) -> Dict[str, Any]:
        return conversation_metadata(conv)

    def _get_conversation_digest(self, conv: Any) -> str:
        return conversation_digest(conv)

    def _get_conversation_timestamp(self, conv: Any) -> float:
        return conversation_timestamp(conv)

    def _conversation_to_text(self, conv: Any) -> str:
        return conversation_to_text(conv)

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
        self._ensure_loaded()
        # Resolve the conversation id / filename stem / path tail to a unique key.
        unique_key = resolve_conv_key(rel_path, self._conversations, self._conv_id_lookup)
        if unique_key is None:
            conv_id = rel_path.split("/")[-1]
            return Observation(kind="error", error=f"conversation not found: {conv_id}")

        conv_text = self._conversation_to_text(self._conversations[unique_key])

        # Apply line range if specified
        if start_line or end_line:
            lines = conv_text.split("\n")
            start = (start_line or 1) - 1
            end = end_line or len(lines)
            conv_text = "\n".join(lines[max(0, start) : min(len(lines), end)])

        # Apply byte limit if specified. A transcript's most useful turns are usually the most
        # recent ones (at the END), so over-budget conversations keep the opening plus the recent
        # tail with the middle elided, instead of a head-only cut that would drop the latest turns.
        if max_bytes and len(conv_text) > max_bytes:
            conv_text = truncate_transcript_middle(conv_text, max_bytes)

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

        self._ensure_loaded()
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

    def _recency_boost(self, cid: str) -> float:
        """Multiplicative recency boost for a conversation. Half-life = 7 days."""
        import math, time as _time
        ts = self._get_conversation_timestamp(self._conversations[cid])
        if ts <= 0:
            return 1.0
        days_old = (_time.time() - ts) / 86400.0
        return 1.0 + math.exp(-days_old * math.log(2) / 7.0)

    def _select_representative_conversations(
        self, conv_ids: List[str], samples_per_cluster: int = 2
    ) -> List[str]:
        """Select representative conversations using TF-DF-IDF + recency."""
        if not conv_ids:
            return []
        return select_representatives(
            conv_ids,
            get_terms=lambda cid: extract_terms(self._get_conversation_digest(self._conversations[cid])),
            samples_per_group=samples_per_cluster,
            get_score_boost=self._recency_boost,
        )

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Pre-flight context: inject digests of Claude sessions relevant to task_text.

        Uses keywords_from_text for natural-language term extraction and
        select_representatives (TF-DF-IDF + recency) to pick the best sessions.
        Never raises.
        """
        try:
            self._ensure_loaded()
            if not self._conversations:
                return AssembledContext()

            query_terms = set(keywords_from_text(task_text))
            conv_kw = {
                cid: set(keywords_from_text(
                    self._get_conversation_digest(conv)
                ))
                for cid, conv in self._conversations.items()
            }

            overlapping = [cid for cid, kw in conv_kw.items() if kw & query_terms]
            if not overlapping:
                return AssembledContext()

            selected = select_representatives(
                items=overlapping,
                get_terms=lambda cid: conv_kw[cid],
                samples_per_group=4,
                get_score_boost=self._recency_boost,
            )

            if not selected:
                return AssembledContext()

            lines = ["--- RELEVANT PAST CLAUDE SESSIONS ---"]
            for cid in selected:
                lines.append(f"[session: {cid}]")
                lines.append(self._get_conversation_digest(self._conversations[cid]))
            return AssembledContext(context_view="\n".join(lines))
        except Exception:
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        pass  # sessions are written by Claude Code; QAR reads them only

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
        - "time_range" (query-aware retrieval routing, spec v3 work package C): optional
          {"start", "end"} HARD filter, applied to ``conv_ids`` BEFORE sampling, using each
          conversation's own timestamp. Degrades to the UNFILTERED conversation set when nothing
          survives (never a silent empty result); the response text is then prefixed with an
          explicit note so the caller can tell a real empty history from a too-narrow filter.
          "topic_terms"/"content_kind"/"actor" are accepted (ignored) -- this adapter does not yet
          rerank by query text (see the "query" key above); a filtered candidate set still lets a
          consumer narrow BY PERIOD even without query-text reranking.
        """
        self._ensure_loaded()
        if not self._conversations:
            return Observation(kind="error", error="no conversations available")

        conv_ids = list(self._conversations.keys())
        degraded_note = ""
        time_range = spec.get("time_range")
        if isinstance(time_range, dict) and (time_range.get("start") or time_range.get("end")):
            start_epoch = parse_date_bound(time_range.get("start"), end_of_day=False)
            end_epoch = parse_date_bound(time_range.get("end"), end_of_day=True)
            if start_epoch is not None or end_epoch is not None:
                time_filtered = [
                    cid for cid in conv_ids
                    if timestamp_in_range(
                        self._get_conversation_timestamp(self._conversations[cid]),
                        start_epoch, end_epoch)
                ]
                if time_filtered:
                    conv_ids = time_filtered
                else:
                    degraded_note = (
                        "No conversations found in the specified time range; showing broader "
                        "results instead.\n\n")

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
            text=degraded_note + "\n\n".join(text_parts),
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
        self._ensure_loaded()
        if not self._conversations:
            return Observation(kind="error", error="no conversations loaded")

        sources = "\n".join([f"{cid}: Claude conversation" for cid in sorted(self._conversations.keys())])
        return Observation(kind="query", text=sources)

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """Describe a conversation by id."""
        self._ensure_loaded()
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
