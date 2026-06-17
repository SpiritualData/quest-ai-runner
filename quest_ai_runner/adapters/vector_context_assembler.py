"""VectorContextAssembler — ContextAssembler backed by a VectorStore.

This assembler uses semantic vector search to retrieve task-relevant context.
It is complementary to ``FileContextStore`` (keyword/IDF): keyword search
catches exact identifiers and symbols; vector search catches semantics and
paraphrase.  Use ``HybridContextAssembler`` to fuse both.

AGENTIC RETRIEVAL FLOW
----------------------
When a ``ModelProvider`` is wired (the ``provider`` constructor arg), the
assembler performs a fully *agentic* retrieval that uses the LLM in two places:

1. **Parallel query generation.** The LLM generates ``num_queries`` diverse
   search queries from the task text (one cheap ``answer`` call).  These are
   searched IN PARALLEL alongside the raw task text.

2. **LLM review / relevance filter.** After deduplication, the LLM reviews the
   candidate hits and selects only those that are genuinely relevant to the
   task.  Irrelevant hits never reach the context_view.

3. **Confidence gate.** Only reviewed-relevant hits whose score exceeds
   ``confidence_min_score`` are injected.  When nothing qualifies the returned
   ``AssembledContext`` is empty and the caller falls back to plain Claude Code
   (the never-worse guarantee).

When no provider is given the agentic steps are skipped: the raw task text is
the only query and all hits above the confidence gate are kept.

AUTO-UPDATE via sync()
----------------------
``record()`` upserts the task text + outcome into the vector store so it
compounds over time: future runs benefit from the grounding accumulated in
prior runs.

MULTI-TENANT SCOPING
--------------------
The ``meta`` dict passed to ``assemble(task_text, meta=...)`` is forwarded as
``scope`` to the vector store, so searches are automatically scoped to the
relevant org / team / quest.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import AssembledContext, ContextAssemblerBase, VectorHit, VectorStore

# Type alias for a seed source callable: returns a list of items suitable for
# ``VectorStore.sync()`` (each has id/text/payload/fingerprint keys).
_SeedSource = Callable[[], List[Dict[str, Any]]]

logger = logging.getLogger(__name__)

# Maximum number of parallel search workers.  Bounded so we don't spawn a
# thread per query on a machine with a tiny thread pool.
_MAX_WORKERS = 8

# Number of lines to show as a text snippet in the context view.
_SNIPPET_LINES = 3


def _snippet(text: str, lines: int = _SNIPPET_LINES) -> str:
    """Return the first ``lines`` non-empty lines of ``text``."""
    parts = [l for l in text.splitlines() if l.strip()][:lines]
    return " | ".join(parts) if parts else text[:120]


def _task_slug(task_text: str) -> str:
    """Derive a stable, short slug from ``task_text`` for use as an association id.

    Lowercases, collapses whitespace, strips punctuation (keeping alphanumerics
    and spaces), and truncates to 80 characters so the slug is stable across
    minor punctuation/case differences but still unique for meaningfully
    different tasks.
    """
    lowered = task_text.lower().strip()
    # Replace any sequence of non-alphanumeric characters with a single space.
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    # Collapse internal whitespace to hyphens to form a readable slug.
    slug = re.sub(r"\s+", "-", cleaned)[:80]
    return slug or "task"


class VectorContextAssembler(ContextAssemblerBase):
    """ContextAssembler that retrieves context via vector (semantic) search.

    Parameters
    ----------
    vector_store:
        Any object satisfying the ``VectorStore`` Protocol.
    provider:
        Optional ``ModelProvider`` used for:
        - LLM query generation (generate diverse queries for better recall)
        - LLM review (filter candidates to only those relevant to the task)
        When ``None`` both steps are skipped.
    query_model:
        Model tier to use for the LLM steps.  Defaults to ``"haiku"`` (cheap).
    num_queries:
        How many extra LLM-generated queries to issue alongside the raw task
        text.  Only used when ``provider`` is given.
    top_k:
        How many nearest neighbours to retrieve per query.
    confidence_min_score:
        Minimum similarity score for a hit to be considered.  Applied after the
        LLM review step (or directly when no provider).  Set to ``0.0`` to keep
        all hits.
    max_in_view:
        Maximum number of hits to include in the rendered context view.
    seed_source:
        Optional callable with no arguments that returns a list of items
        suitable for ``VectorStore.sync()`` (each with id/text/payload/
        fingerprint keys).  When set, the FIRST ``assemble()`` call in this
        process seeds the vector store by calling
        ``self._store.sync(seed_source(), scope=None)`` before searching.
        This solves the cold-start problem: on a fresh repo the store is empty
        and vector orientation does nothing until tasks accumulate. Providing a
        ``FileContextStore.export_for_embedding`` as the seed source embeds the
        docstring-card descriptions so semantic search works immediately.
        Because ``sync`` is fingerprint-based, subsequent calls only re-embed
        changed cards (AUTO-UPDATE). The seeding is best-effort: any error is
        silently swallowed. The guard fires ONCE per process (instance flag).
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        provider: Any = None,
        query_model: str = "haiku",
        num_queries: int = 3,
        top_k: int = 8,
        confidence_min_score: float = 0.0,
        max_in_view: int = 8,
        half_life_days: float = 30.0,
        max_associations: int = 500,
        seed_source: Optional[_SeedSource] = None,
        _clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._store = vector_store
        self._provider = provider
        self._query_model = query_model
        self._num_queries = num_queries
        self._top_k = top_k
        self._confidence_min_score = confidence_min_score
        self._max_in_view = max_in_view
        self._half_life_days = half_life_days
        self._max_associations = max_associations
        self._seed_source: Optional[_SeedSource] = seed_source
        # Guard: cold-start seeding runs at most once per process (per instance).
        self._seed_done: bool = False
        # Injectable clock for deterministic tests; defaults to time.time.
        self._clock: Callable[[], float] = _clock if _clock is not None else time.time

    # ------------------------------------------------------------------
    # ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def _maybe_seed(self) -> None:
        """Seed the vector store from ``seed_source`` on the first assemble call.

        Uses ``VectorStore.sync()`` so only new or changed items are embedded
        (fingerprint-based AUTO-UPDATE). Best-effort: any error is swallowed.
        The guard fires at most once per instance.
        """
        if self._seed_done:
            return
        if self._seed_source is None:
            self._seed_done = True
            return
        try:
            items = self._seed_source()
            if items:
                self._store.sync(items, scope=None)
                # Only consider seeding DONE once we actually had items to seed. The keyword
                # bootstrap runs in a background thread, so an early assemble() can see an empty
                # source; in that case leave the guard unset so a later assemble() (after
                # bootstrap finishes) seeds. sync() is fingerprint-based, so re-running it after
                # items exist is cheap and idempotent.
                self._seed_done = True
        except Exception:  # noqa: BLE001
            # A hard failure is final (don't retry a broken source forever).
            self._seed_done = True

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Retrieve and render task-relevant context via vector search.  Never raises."""
        try:
            self._maybe_seed()
        except Exception:  # noqa: BLE001
            pass
        try:
            return self._assemble_inner(task_text, meta=meta)
        except Exception:
            logger.debug("VectorContextAssembler.assemble failed", exc_info=True)
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Upsert a TASK-TO-CONTEXT ASSOCIATION so the store compounds over time.  Never raises.

        The embedded text = ``task_text`` + a short structural description of the region used
        (file paths + their summaries/symbols from ``outcome``).  This builds a searchable
        mapping from "things this kind of task does" to "where the work lives", so a future
        similar task retrieves the right region immediately via vector search.

        The payload is rich: ``{paths, symbols, summary, task, kind}`` so a top hit gives
        the retrieval agent directly actionable metadata.

        If a ``ModelProvider`` (``provider``) is wired, one cheap LLM call generates a
        one-line orientation summary to embed instead of the structural description.  This is
        best-effort: if the call fails the structural description is used.
        """
        try:
            self._record_inner(task_text, outcome)
        except Exception:
            logger.debug("VectorContextAssembler.record failed", exc_info=True)

    def _record_inner(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Actual record logic.  May raise; callers wrap in try/except."""
        scope = outcome.get("scope") or None

        # --- DEDUP: stable association id derived from a slug of the task text.
        # Same task -> same id -> upsert overwrites rather than duplicates.
        slug = _task_slug(task_text)
        item_id = f"assoc:{slug}"

        # Collect paths and symbols from outcome.
        paths: List[str] = list(outcome.get("files") or [])
        symbols: List[str] = list(outcome.get("symbols") or [])

        # Build a short structural description of the region used.
        region_parts: List[str] = []
        if paths:
            region_parts.append("files: " + ", ".join(paths[:8]))
        if symbols:
            region_parts.append("symbols: " + ", ".join(symbols[:8]))
        region_desc = "; ".join(region_parts) if region_parts else ""

        # Build the text to embed: task + region.
        embed_text = task_text
        if region_desc:
            embed_text = f"{task_text} {region_desc}"

        # Optionally generate a one-line LLM summary (cheap, best-effort).
        if self._provider is not None and region_desc:
            try:
                prompt = (
                    f"Write ONE short sentence (max 20 words) describing what this task does "
                    f"and which code region it touches.  No lists.\n\n"
                    f"Task: {task_text}\nRegion: {region_desc}"
                )
                llm_summary = self._provider.answer(
                    [{"role": "user", "content": prompt}],
                    model=self._query_model,
                )
                llm_summary = llm_summary.strip()
                if llm_summary:
                    embed_text = llm_summary
            except Exception:
                pass  # fall back to structural description

        # --- TIMESTAMP: use provided ts for test determinism; fall back to now.
        now_ts = outcome.get("ts")
        if now_ts is None:
            now_ts = self._clock()
        now_ts = float(now_ts)

        # --- MERGE: bump count on re-record (the upsert overwrites by id).
        # We don't do a read-before-write to keep the never-raises contract simple;
        # the payload always carries the latest count from the outcome if provided,
        # else we increment naively via a sentinel in the outcome dict.
        count = int(outcome.get("_count", 1))

        # Build rich payload so a top hit gives the agent directly useful metadata.
        summary_for_payload = (
            outcome.get("summary")
            or (f"{task_text[:80]} -> {region_desc[:80]}" if region_desc else task_text[:120])
        )
        payload: Dict[str, Any] = {
            "task": task_text,
            "paths": paths,
            "symbols": symbols,
            "summary": summary_for_payload,
            "kind": outcome.get("kind"),
            "ts": now_ts,
            "count": count,
        }

        self._store.upsert(
            [{"id": item_id, "text": embed_text, "payload": payload}],
            scope=scope,
        )

        # --- CAPACITY BOUND: evict oldest if over the cap.
        # Best-effort: only when the store advertises count/evict_oldest.
        try:
            if (
                hasattr(self._store, "count")
                and hasattr(self._store, "evict_oldest")
                and callable(self._store.count)  # type: ignore[union-attr]
                and callable(self._store.evict_oldest)  # type: ignore[union-attr]
            ):
                current = self._store.count(scope=scope)  # type: ignore[union-attr]
                overflow = current - self._max_associations
                if overflow > 0:
                    self._store.evict_oldest(overflow, scope=scope)  # type: ignore[union-attr]
        except Exception:
            pass  # capacity eviction is best-effort; never raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_queries(self, task_text: str) -> List[str]:
        """Use the LLM to generate diverse search queries.

        Returns a list of query strings (may be empty on failure).  Never raises.
        """
        if self._provider is None:
            return []
        try:
            prompt = (
                f"Generate {self._num_queries} short, diverse search queries that "
                f"would help retrieve relevant context for the following task. "
                f"Output one query per line, no numbering, no extra text.\n\n"
                f"Task: {task_text}"
            )
            raw = self._provider.answer(
                [{"role": "user", "content": prompt}],
                model=self._query_model,
            )
            queries = [
                line.strip()
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            return queries[: self._num_queries]
        except Exception:
            logger.debug("VectorContextAssembler._generate_queries failed", exc_info=True)
            return []

    def _search_parallel(
        self,
        queries: List[str],
        scope: Optional[Dict[str, Any]],
    ) -> List[VectorHit]:
        """Search all queries IN PARALLEL; dedupe hits by id keeping best score."""
        if not queries:
            return []

        best: Dict[str, VectorHit] = {}

        def _search_one(q: str) -> List[VectorHit]:
            try:
                return self._store.search(q, scope=scope, top_k=self._top_k)
            except Exception:
                return []

        n_workers = min(_MAX_WORKERS, len(queries))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(_search_one, q) for q in queries]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        for hit in fut.result():
                            existing = best.get(hit.id)
                            if existing is None or hit.score > existing.score:
                                best[hit.id] = hit
                    except Exception:
                        pass
        except Exception:
            # Fall back to serial search.
            for q in queries:
                for hit in _search_one(q):
                    existing = best.get(hit.id)
                    if existing is None or hit.score > existing.score:
                        best[hit.id] = hit

        return list(best.values())

    def _llm_review(
        self,
        task_text: str,
        candidates: List[VectorHit],
    ) -> List[VectorHit]:
        """Ask the LLM to select which candidates are relevant to the task.

        Returns the filtered list (same objects, different subset).  If the
        provider is unavailable or the call fails, returns all candidates.
        """
        if self._provider is None or not candidates:
            return candidates
        try:
            items_text = "\n".join(
                f"[{i}] id={h.id} | score={h.score:.3f} | {_snippet(h.text or str(h.payload))}"
                for i, h in enumerate(candidates)
            )
            prompt = (
                f"You are reviewing candidate context items for relevance to a task.\n\n"
                f"Task: {task_text}\n\n"
                f"Candidates:\n{items_text}\n\n"
                f"Output ONLY the indices (comma-separated) of the items that are genuinely "
                f"relevant to the task. If none are relevant, output 'none'."
            )
            raw = self._provider.answer(
                [{"role": "user", "content": prompt}],
                model=self._query_model,
            )
            raw = raw.strip().lower()
            if raw == "none" or not raw:
                return []
            # Parse indices; ignore anything that doesn't parse as int.
            kept_indices = set()
            for part in raw.replace(";", ",").split(","):
                part = part.strip()
                try:
                    idx = int(part)
                    if 0 <= idx < len(candidates):
                        kept_indices.add(idx)
                except ValueError:
                    pass
            if not kept_indices:
                return []
            return [candidates[i] for i in sorted(kept_indices)]
        except Exception:
            logger.debug("VectorContextAssembler._llm_review failed", exc_info=True)
            return candidates  # on failure keep all (best-effort)

    def _render_hits(self, hits: List[VectorHit]) -> str:
        """Render a list of VectorHits into a human-readable context view string.

        When a hit carries a rich task-to-context payload (``paths``, ``symbols``,
        ``task``, ``summary``), those fields are surfaced so the consuming agent
        immediately knows where to read.  The age of the association (derived from
        the ``ts`` payload field) is shown when present.
        """
        now = self._clock()
        parts: List[str] = []
        for hit in hits:
            part_lines = [f"### Vector hit: {hit.id}  (score={hit.score:.3f})"]
            # Age annotation from ts payload field.
            ts = hit.payload.get("ts") if hit.payload else None
            if ts is not None:
                try:
                    age_days = max(0.0, (now - float(ts)) / 86400.0)
                    if age_days < 1.0:
                        age_label = "from a task today"
                    elif age_days < 2.0:
                        age_label = "from a task 1 day ago"
                    else:
                        age_label = f"from a task {int(age_days)} days ago"
                    part_lines.append(f"  age: {age_label}")
                except (TypeError, ValueError):
                    pass
            # Rich task-to-context payload fields.
            task_text = hit.payload.get("task", "") or ""
            if task_text:
                part_lines.append(f"  matched task: {task_text[:120]}")
            summary = hit.payload.get("summary", "") or ""
            if summary:
                part_lines.append(f"  summary: {summary[:160]}")
            hit_paths = hit.payload.get("paths") or []
            if hit_paths:
                part_lines.append(f"  read these files: {', '.join(hit_paths[:8])}")
            hit_syms = hit.payload.get("symbols") or []
            if hit_syms:
                part_lines.append(f"  symbols: {', '.join(hit_syms[:8])}")
            # Legacy single-path field.
            path = hit.payload.get("path", "") or ""
            if path and path not in hit_paths:
                part_lines.append(f"  path: {path}")
            # Text snippet (for non-association hits).
            if hit.text and not task_text:
                snippet = _snippet(hit.text)
                if snippet:
                    part_lines.append(f"  text: {snippet}")
            parts.append("\n".join(part_lines))
        return "\n\n---\n\n".join(parts)

    def _assemble_inner(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        scope = meta or None

        # Step a: build query list.
        extra_queries = self._generate_queries(task_text)
        all_queries = [task_text] + extra_queries

        # Step b: vector-search all queries IN PARALLEL; dedupe.
        candidates = self._search_parallel(all_queries, scope)

        if not candidates:
            return AssembledContext()

        # Step c: LLM review when provider is available.
        if self._provider is not None:
            candidates = self._llm_review(task_text, candidates)

        # Step d: recency decay — re-rank by age-decayed effective score.
        now = self._clock()
        half_life = self._half_life_days
        decayed: List[tuple] = []  # (effective_score, hit)
        for h in candidates:
            ts = h.payload.get("ts") if h.payload else None
            if ts is not None:
                try:
                    age_days = max(0.0, (now - float(ts)) / 86400.0)
                except (TypeError, ValueError):
                    age_days = None
            else:
                age_days = None

            if age_days is not None:
                effective = h.score * (0.5 ** (age_days / half_life))
            else:
                # No ts: neutral mild decay (treat as 1 half-life old).
                effective = h.score * 0.5

            decayed.append((effective, h))

        # Confidence gate applies to effective (decayed) score.
        kept_decayed = [
            (eff, h) for eff, h in decayed if eff >= self._confidence_min_score
        ]
        if not kept_decayed:
            return AssembledContext()

        # Sort by effective score descending, cap at max_in_view.
        kept_decayed.sort(key=lambda x: x[0], reverse=True)
        kept_decayed = kept_decayed[: self._max_in_view]
        kept = [h for _, h in kept_decayed]

        # Step e: render.
        context_view = self._render_hits(kept)
        card_ids = [h.id for h in kept]

        # --- Context transparency: classify each hit as task_memory vs vector bootstrap --------
        # Hits whose payload carries a "task" field are prior-task associations (task_memory);
        # others are bootstrap card embeddings (vector).  Group them into per-type source entries
        # so the orchestrator can emit a human-readable summary, e.g.
        # "Context from: semantic match (model_registry.py), prior task 2 days ago."
        now = self._clock()
        _vector_items: List[str] = []
        _task_memory_entries: List[dict] = []
        for h in kept:
            payload = h.payload or {}
            if payload.get("task"):
                # Task-memory hit: compute age label.
                ts = payload.get("ts")
                if ts is not None:
                    try:
                        age_days = max(0.0, (now - float(ts)) / 86400.0)
                        if age_days < 1.0:
                            age_label = "prior task today"
                        elif age_days < 2.0:
                            age_label = "prior task 1 day ago"
                        else:
                            age_label = f"prior task {int(age_days)} days ago"
                    except (TypeError, ValueError):
                        age_label = "prior task"
                else:
                    age_label = "prior task"
                hit_paths = payload.get("paths") or []
                _task_memory_entries.append(
                    {"adapter": "task_memory", "label": age_label, "items": list(hit_paths)}
                )
            else:
                # Bootstrap card hit: collect paths from payload.
                hit_paths = payload.get("paths") or []
                _vector_items.extend(p for p in hit_paths if p not in _vector_items)

        _sources: List[dict] = []
        if _vector_items:
            _sources.append({"adapter": "vector", "label": "semantic match", "items": _vector_items})
        _sources.extend(_task_memory_entries)

        # --- Card metadata: populate selection info for UI display and transparency ---------
        # Build metadata for each selected vector hit so the orchestrator can emit which cards were chosen.
        card_metadata: List[Dict[str, Any]] = []
        for h in kept:
            payload = h.payload or {}
            hit_paths = payload.get("paths") or []
            # Determine adapter type from payload
            adapter_type = "task_memory" if payload.get("task") else "vector"
            card_metadata.append({
                "id": h.id,
                "title": h.text[:100] if h.text else "(no text)",  # first 100 chars as title
                "relevance_score": min(1.0, h.score),  # normalize vector scores
                "file_count": len(hit_paths),
                "files": hit_paths[:3],  # top 3 files
                "adapter": adapter_type,
            })

        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=[],
            sources=_sources,
            card_metadata=card_metadata,
        )
