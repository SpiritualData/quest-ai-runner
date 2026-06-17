"""Composite adapter that runs multiple RetrievalAdapters in parallel and merges results.

This enables a single RetrievalAdapter parameter to query multiple sources simultaneously:
files, databases, Claude conversations, vector stores, task memory, etc. Each adapter
runs in its own thread; results are merged by kind (reads grouped by source, greps
deduplicated, queries combined).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import Observation, RetrievalAdapter


class CompositeRetrievalAdapter:
    """Parallel multi-source retrieval.

    Runs a list of RetrievalAdapters in parallel, merging results intelligently:
    - reads: grouped by adapter name + path
    - greps: deduplicated by pattern + hit
    - queries: combined with source attribution
    - discovery: merged and deduplicated

    Example:
        >>> files = FilesAdapter("/docs")
        >>> db = CachedDbAdapter(...)
        >>> conversations = ClaudeConversationsAdapter(...)
        >>> retrieval = CompositeRetrievalAdapter([files, db, conversations])
    """

    def __init__(self, adapters: List[RetrievalAdapter], max_workers: int = 4):
        """Initialize with a list of adapters.

        Args:
            adapters: List of RetrievalAdapter instances to query in parallel.
            max_workers: Max threads for parallel execution (default 4).
        """
        if not adapters:
            raise ValueError("CompositeRetrievalAdapter requires at least one adapter")
        self.adapters = adapters
        self.max_workers = min(max_workers, len(adapters))

    def _adapter_name(self, adapter: RetrievalAdapter) -> str:
        """Get a friendly name for an adapter."""
        return getattr(adapter, "__class__", type(adapter)).__name__

    def _run_all(self, method_name: str, *args, **kwargs) -> List[tuple]:
        """Run a method on all adapters in parallel. Returns [(adapter, result), ...]."""
        results: List[tuple] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(getattr(adapter, method_name), *args, **kwargs): adapter
                for adapter in self.adapters
            }
            for future in as_completed(futures):
                adapter = futures[future]
                try:
                    result = future.result()
                    results.append((adapter, result))
                except Exception:  # noqa: BLE001
                    # Silently skip broken adapters; return an empty observation
                    results.append((adapter, Observation(kind="error", error="adapter error")))
        return results

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Read from the first adapter that has the path; fall back through adapters."""
        # Try each adapter in order until one succeeds (returns non-error).
        for adapter in self.adapters:
            obs = adapter.read_section(
                rel_path, start_line=start_line, end_line=end_line, heading=heading, max_bytes=max_bytes
            )
            if obs.kind != "error":
                # Mark the source adapter for transparency
                return obs

        # All adapters failed; return error
        return Observation(
            kind="error",
            error=f"path not found in any adapter: {rel_path}",
        )

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Grep across all adapters in parallel, deduplicate and merge hits."""
        results = self._run_all("grep", pattern, scope=scope, max_hits=max_hits)

        # Collect all hits, deduped by (hit_text, source adapter)
        all_hits: List[Dict[str, Any]] = []
        seen: set = set()

        for adapter, obs in results:
            if obs.kind == "error":
                continue
            adapter_name = self._adapter_name(adapter)
            for hit in obs.hits:
                # Dedup by hit line + adapter (same line from different adapters is kept)
                hit_key = (hit.get("line", ""), adapter_name)
                if hit_key not in seen:
                    seen.add(hit_key)
                    hit_with_source = {**hit, "_source": adapter_name}
                    all_hits.append(hit_with_source)

        if not all_hits:
            return Observation(
                kind="error",
                pattern=pattern,
                error=f"pattern not found: {pattern}",
            )

        # Limit total hits if requested
        if max_hits and len(all_hits) > max_hits:
            all_hits = all_hits[:max_hits]

        return Observation(
            kind="grep",
            pattern=pattern,
            hits=all_hits,
        )

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Query all adapters in parallel. Combine results with source attribution."""
        results = self._run_all("query", spec)

        # Collect all query results
        combined_text_parts: List[str] = []
        all_hits: List[Dict[str, Any]] = []
        seen_errors: List[str] = []

        for adapter, obs in results:
            if obs.kind == "error":
                seen_errors.append(f"{self._adapter_name(adapter)}: {obs.error}")
                continue

            # Add this adapter's results with source attribution
            if obs.text:
                combined_text_parts.append(f"[{self._adapter_name(adapter)}]\n{obs.text}")
            if obs.hits:
                for hit in obs.hits:
                    hit_with_source = {**hit, "_source": self._adapter_name(adapter)}
                    all_hits.append(hit_with_source)

        if not combined_text_parts and not all_hits:
            error_msg = "; ".join(seen_errors) if seen_errors else "no results from any adapter"
            return Observation(kind="error", error=error_msg)

        combined_text = "\n\n".join(combined_text_parts)
        return Observation(
            kind="query",
            text=combined_text,
            hits=all_hits if all_hits else [],
        )

    def list_sources(self) -> Observation:
        """List sources from all adapters, deduplicated."""
        results = self._run_all("list_sources")

        # Combine all source listings
        all_sources: Dict[str, str] = {}  # name -> description

        for adapter, obs in results:
            if obs.kind == "error":
                continue
            # Parse the text (format: "name: description" per line)
            if obs.text:
                for line in obs.text.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        name = parts[0].strip()
                        desc = parts[1].strip()
                        if name not in all_sources:
                            all_sources[name] = desc

        if not all_sources:
            return Observation(kind="error", error="no sources found in any adapter")

        combined = "\n".join(f"{name}: {desc}" for name, desc in sorted(all_sources.items()))
        return Observation(kind="query", text=combined)

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """Describe a source. Try adapters in order until one succeeds."""
        for adapter in self.adapters:
            obs = adapter.describe_source(name, path=path)
            if obs.kind != "error":
                return obs

        return Observation(
            kind="error",
            error=f"source not found: {name}",
        )

    def list_operations(self) -> Observation:
        """List operations from all adapters, deduplicated."""
        results = self._run_all("list_operations")

        all_ops: Dict[str, str] = {}  # name -> description

        for adapter, obs in results:
            if obs.kind == "error":
                continue
            if obs.text:
                for line in obs.text.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        name = parts[0].strip()
                        desc = parts[1].strip()
                        if name not in all_ops:
                            all_ops[name] = desc

        if not all_ops:
            return Observation(kind="error", error="no operations found in any adapter")

        combined = "\n".join(f"{name}: {desc}" for name, desc in sorted(all_ops.items()))
        return Observation(kind="query", text=combined)

    def describe_operation(self, name: str) -> Observation:
        """Describe an operation. Try adapters in order until one succeeds."""
        for adapter in self.adapters:
            obs = adapter.describe_operation(name)
            if obs.kind != "error":
                return obs

        return Observation(
            kind="error",
            error=f"operation not found: {name}",
        )
