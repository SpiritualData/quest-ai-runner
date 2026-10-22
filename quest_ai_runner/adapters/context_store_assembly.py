"""Assembling context for a request: scoring, fallback file search and source-usage tracking.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path
from datetime import datetime, timezone

from ..core.adapters import AssembledContext
from ..core.background_pacing import Pacer
from .card_content_render import (
    content_item_text as _content_item_text,
    filter_content_by_time_range,
    mark_items_used as _mark_items_used,
    normalize_content as _normalize_content,
    render_card_content_blocks,
    tokenize as _tokenize,
)
from ._walk import effective_skip_dirs, prune_dirnames
from ..core.scope_tags import scope_tags_allow
from .context_card_extraction import (
    _QUEST_FOLDER_BOOST,
    _RECENCY_BOOST_HALF_LIFE_DAYS,
    _RECENCY_BOOST_USAGE_CAP,
    _SOURCE_FILE_WEIGHT,
    _SOURCE_USAGE_MIN_INTERVAL_SECONDS,
    _card_covered_paths,
    _card_display_title,
)
from .context_bootstrap_support import _BOOTSTRAP_MAX_BYTES, _SOURCE_EXTS, _log, _run_parallel


class AssemblyMixin:
    """Assembling context for a request: scoring, fallback file search and source-usage tracking."""

    def register_reference_resolver(
        self, ref_type: str, resolver: Any, *, override: bool = False
    ) -> None:
        """Wire a resolver for content ``type`` ``ref_type`` AFTER construction. Best-effort.

        Lets a caller add a resolver for a resolvable adapter that is only built later than this store
        (e.g. an adapter whose own construction depends on this store). ``resolver`` is either a
        ReferenceResolver object (has ``resolve(locator, *, max_chars)``) or a bare callable with that
        same signature -- e.g. an adapter's ``resolve_reference`` bound method. By default an existing
        wired resolver for the type is KEPT (so a consumer-injected one always wins); pass
        ``override=True`` to replace it. A callable is adapted to the ReferenceResolver surface. Never
        raises."""
        try:
            if not isinstance(ref_type, str) or not ref_type or resolver is None:
                return
            if ref_type in self._resolvers and not override:
                return
            from .reference_resolver import coerce_resolver
            coerced = coerce_resolver(resolver)
            if coerced is not None:
                self._resolvers[ref_type] = coerced
        except Exception:  # noqa: BLE001 — resolver registration must never break a run
            pass

    def _recency_boost_factor(self, card: Dict[str, Any]) -> float:
        """A small bounded multiplier (1.0 .. 1.0+max) that nudges a recently-used / recently-updated
        card up the RANKING, used ONLY after the confidence gate has passed on the un-boosted score.

        Blends two real signals, each normalized to 0..1: usage (``usage_count`` saturating at
        ``_RECENCY_BOOST_USAGE_CAP``) and recency (the newest content ``ts``, decayed by a 30-day
        half-life). With no usage and no timestamped content the factor is exactly 1.0 (no effect),
        so cards without history rank purely on relevance. Returns 1.0 when the boost is disabled
        (max == 0.0). Never raises.
        """
        if self._recency_boost_max <= 0.0:
            return 1.0
        try:
            usage = float(card.get("usage_count", 0) or 0.0)
            usage_norm = (min(usage, _RECENCY_BOOST_USAGE_CAP) / _RECENCY_BOOST_USAGE_CAP
                          if _RECENCY_BOOST_USAGE_CAP > 0 else 0.0)
            content = card.get("content") or []
            ts_values = [float(it.get("ts") or 0.0) for it in content if isinstance(it, dict)]
            newest = max(ts_values) if ts_values else 0.0
            recency_norm = 0.0
            if newest > 0.0:
                now = datetime.now(timezone.utc).timestamp()
                age_days = max(0.0, (now - newest) / 86400.0)
                recency_norm = 0.5 ** (age_days / _RECENCY_BOOST_HALF_LIFE_DAYS)
            signal = 0.5 * usage_norm + 0.5 * recency_norm   # 0..1
            return 1.0 + self._recency_boost_max * signal
        except Exception:  # noqa: BLE001
            return 1.0

    @staticmethod
    def _card_pins_folder(card: Dict[str, Any], folder_prefix: str) -> bool:
        """True if any file this card pins falls under ``folder_prefix`` (a repo-root-relative
        POSIX path, no trailing slash)."""
        prefix_with_slash = folder_prefix.rstrip("/") + "/"
        for fe in card.get("files", []):
            p = fe.get("path", "")
            if p and (p == folder_prefix or p.startswith(prefix_with_slash)):
                return True
        return False

    # ------------------------------------------------------------------
    # Public API: ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Select and render relevant cards for ``task_text``. Never raises.

        On the first call, if ``auto_bootstrap`` is enabled and the cards
        directory is empty (or absent), triggers ``bootstrap()`` once to seed
        the store from the repo tree before scoring.  The guard fires only once
        per instance regardless of outcome.
        """
        try:
            self._maybe_auto_bootstrap()
        except Exception:  # noqa: BLE001
            pass
        try:
            result = self._assemble_inner(task_text, meta=meta)
        except Exception:  # noqa: BLE001
            result = AssembledContext()
        return self._with_backend_health(result)

    def _with_backend_health(self, result: AssembledContext) -> AssembledContext:
        """Make a failing card backend VISIBLE in the run: when the repository reports itself degraded
        (a repository exposing ``health()``, e.g. ``QuestApiCardRepository``), add a source entry that
        the orchestrator emits as a status event, so "no cards" is never silently a backend outage.
        Never raises."""
        try:
            health = getattr(self._repo, "health", None)
            report = health() if callable(health) else None
            if isinstance(report, dict) and report.get("degraded"):
                last = report.get("last_error") or {}
                reason = f"{last.get('method', '')} {last.get('path', '')} status {last.get('status', '')}: " \
                         f"{last.get('detail', '')}".strip()
                result.sources.append({
                    "adapter": "card_backend",
                    "label": f"CARD BACKEND ERROR ({report.get('error_count', 0)} failure(s); "
                             f"cards may be missing or stale): {reason}",
                    "items": [],
                })
        except Exception:  # noqa: BLE001
            _log.debug("card backend health check failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _card_term_weights(self, card: Dict[str, Any]) -> Dict[str, float]:
        """Build a term -> max-weight map for field-weighted scoring.

        Each term's weight is the MAXIMUM across all sources it appears in:
          keywords (LLM-tagged)    3.0  -- most intentional signal
          summary + filename stem  2.0  -- filename is a title for the file
          symbols (fn/class names) 1.0  -- meaningful but uncurated
          directory components     0.5  -- structural noise
          file extension           0.0  -- dropped (.py/.ts adds no signal)

        Max (not sum) prevents inflating scores when a keyword term also appears
        in the summary -- the keyword weight already captures that signal.

        A LEARNED card (one an updater wrote from a finished run, whose knowledge is typed content
        REFERENCES rather than bootstrapped file entries) carries ``name``/``description`` instead of
        ``keywords``/``summary``, and its file paths live in content locators, not in ``files``.
        Those fields are indexed here at the same intent-weighted levels, so such a card is scored on
        the same footing as a bootstrapped one instead of being systematically under-scored and
        filtered out below the confidence gate (which made a run's learned references unreachable to
        the next run).
        """
        weights: Dict[str, float] = {}

        def _add(text: str, w: float) -> None:
            for t in _tokenize(text):
                if w > weights.get(t, 0.0):
                    weights[t] = w

        for kw in card.get("keywords", []):
            _add(kw, 3.0)
        # ``name`` is as intentional a signal as an LLM keyword (someone named the topic); the
        # ``description`` sits at summary level. Both are no-ops on a card that has neither.
        _add(card.get("name", ""), 3.0)
        _add(card.get("summary", ""), 2.0)
        _add(card.get("description", ""), 2.0)

        for fe in card.get("files", []):
            path = fe.get("path", "")
            if path:
                p = Path(path)
                # Filename stem (e.g. "ChatWindow" from ChatWindow.tsx) → title-level signal
                _add(re.sub(r"[_\-]", " ", p.stem), 2.0)
                # Directory components → structural noise
                for part in p.parent.parts:
                    _add(re.sub(r"[_\-]", " ", part), 0.5)
            # TF-DF-IDF terms: top-15 distinctive terms from actual file content.
            # Weight 2.5 — above filename (2.0) and symbols (1.0), below LLM keywords (3.0).
            # These are only present in cards bootstrapped at v4+; older cards degrade gracefully.
            for term in fe.get("tfdfidf_terms", []):
                _add(term, 2.5)
            for sym in fe.get("symbols", []):
                _add(sym, 1.0)

        # Source-agnostic CONTENT items: tokenize each item's TARGET (the locator: a file path, a
        # collection name/id), its ``why``, and any note text, so a card with ZERO files (a pure
        # reference/note card, e.g. one a deep run's future context produced) is selectable by IDF
        # and vectors. Weight 2.0 — title-level signal, on par with the summary, below keywords.
        # A file reference's PATH is part of that item text (``locator_label``), so "where does the
        # relay config live" reaches a card that points at ``config/relay.toml`` even though the
        # card pins no files of its own. Structural path components (``src``, ``lib``) are common
        # across cards, so IDF damps them on its own; no extra down-weighting is needed.
        for item in _normalize_content(card.get("content")):
            _add(_content_item_text(item), 2.0)

        return weights

    def _scoring_index(self, cards: Dict[str, Dict[str, Any]], pace: Optional[Pacer] = None
                       ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, int]]:
        """``(term-weight map per card, presence-based document frequency)`` for ``cards``.

        Cached across turns: a card's weights are reused while it is the same object (the card
        cache hands out new dicts whenever it reloads), and the DF table while the card set is
        unchanged and no card's weights were recomputed.

        ``pace`` is called once per card weighed. The default does not pause: someone is waiting.
        A build that nobody waits for any more (the turn gave up on it at its deadline, or this is
        the prewarm) passes a ``Pacer`` so it stops starving the threads that are.
        """
        pace = pace or Pacer.never()
        cache = self._term_weight_cache
        maps: Dict[str, Dict[str, float]] = {}
        recomputed = False
        for cid, card in cards.items():
            hit = cache.get(cid)
            if hit is not None and hit[0] is card:
                maps[cid] = hit[1]
            else:
                pace()
                weights = self._card_term_weights(card)
                cache[cid] = (card, weights)
                maps[cid] = weights
                recomputed = True
        if len(cache) > 2 * max(len(cards), 1):
            for stale_id in [k for k in cache if k not in cards]:
                del cache[stale_id]
        key = frozenset(cards)
        if not recomputed and self._df_cache is not None and self._df_cache[0] == key:
            return maps, self._df_cache[1]
        df: Dict[str, int] = {}
        for tw in maps.values():
            for term in tw:
                df[term] = df.get(term, 0) + 1
        self._df_cache = (key, df)
        return maps, df

    def _card_searchable_terms(self, card: Dict[str, Any]) -> Set[str]:
        """Return the set of all terms in a card (for DF computation)."""
        return set(self._card_term_weights(card).keys())

    # Hard cap on this fallback, independent of any gating a consumer wires (see
    # ``_assemble_inner``'s ``skip_corpus_fallback`` meta key below). Halved from the original
    # 20 files walked / 12 shown / 6 lines each (2026-10-06): this path fires precisely when NO
    # card matched, so it has no relevance signal of its own beyond "the keyword is somewhere in
    # the corpus" -- a turn that is not about this corpus at all (a Quest-data question, a
    # personal aside) can still share a few common words with some file, and the old caps could
    # inject a dozen files' worth of unrelated lines into a turn the corpus has nothing to do
    # with. Never raised back up without a real relevance signal to go with it.
    FALLBACK_MAX_FILES_WALKED = 10

    FALLBACK_MAX_FILES_SHOWN = 6

    FALLBACK_MAX_LINES_PER_FILE = 3

    def _fallback_file_search(self, task_kws: Set[str]) -> AssembledContext:
        """When no cards score above threshold, grep the raw file corpus for query keywords.

        This gives the brain relevant file snippets even when the card index is cold or
        misses novel component names, class names, or camelCase identifiers.  Returns an
        empty AssembledContext when no files are reachable or no hits are found.

        Hard-capped (``FALLBACK_MAX_*`` above) because this path has no relevance signal beyond
        "the keyword appears somewhere in the corpus" -- see ``_assemble_inner`` for the separate,
        OPTIONAL ``skip_corpus_fallback`` meta gate a consumer with its own "is this turn even
        about this corpus" signal (a reach judge, a classifier verdict) can set to skip this path
        entirely instead of merely shrinking it.
        """
        if self._repo_root is None or not self._repo_root.is_dir():
            return AssembledContext()

        kw_list = sorted(task_kws, key=len, reverse=True)[:12]
        if not kw_list:
            return AssembledContext()

        pattern = "|".join(re.escape(k) for k in kw_list)
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return AssembledContext()

        skip_dirs = effective_skip_dirs(self._repo_root)
        hits_by_file: Dict[str, List[str]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self._repo_root):
                prune_dirnames(dirnames, current=Path(dirpath), base_skip=skip_dirs)
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    fpath = Path(dirpath) / fn
                    if fpath.suffix not in _SOURCE_EXTS:
                        continue
                    try:
                        if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                            continue
                        text = fpath.read_text(encoding="utf-8", errors="replace")
                        matching = [ln.rstrip() for ln in text.splitlines() if rx.search(ln)]
                        if matching:
                            rel = str(fpath.relative_to(self._repo_root))
                            hits_by_file[rel] = matching[: self.FALLBACK_MAX_LINES_PER_FILE]
                    except OSError:
                        pass
                    if len(hits_by_file) >= self.FALLBACK_MAX_FILES_WALKED:
                        break
                if len(hits_by_file) >= self.FALLBACK_MAX_FILES_WALKED:
                    break
        except Exception:  # noqa: BLE001
            return AssembledContext()

        if not hits_by_file:
            return AssembledContext()

        _log.info("context index: no cards matched; fallback file search found %d file(s)",
                  len(hits_by_file))
        parts = [
            "No context cards matched this query. Relevant lines found by direct file search:\n"
        ]
        for rel, lines in sorted(hits_by_file.items())[: self.FALLBACK_MAX_FILES_SHOWN]:
            parts.append(f"**{rel}**")
            for ln in lines:
                snippet = ln[:200].rstrip() + ("…" if len(ln) > 200 else "")
                parts.append(f"  {snippet}")
            parts.append("")
        return AssembledContext(
            context_view="\n".join(parts),
            sources=[{"adapter": "fallback_grep", "label": "direct file search",
                      "items": list(hits_by_file.keys())}],
        )

    def _assemble_inner(self, task_text: str, *,
                        meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        task_kws = _tokenize(task_text)
        if not task_kws and not (meta or {}).get("priority_card_ids"):
            return AssembledContext()

        # QUEST-FOLDER SCOPING: when this run's meta carries a goal/quest id that quest_folder_map
        # maps to a folder, resolve it ONCE to the repo-root-relative prefix cards are matched
        # against. goal_id is checked FIRST, mirroring the poller's _quest_folder_for: a personal
        # "goal is the hub" task carries its id there (often with no quest_id at all). None (the
        # common case: no map, or no meta, or neither id is mapped) is a no-op.
        folder_prefix: Optional[str] = None
        if meta and self._quest_folder_map:
            for _id_key in ("goal_id", "quest_id"):
                _id = meta.get(_id_key)
                if _id and str(_id) in self._quest_folder_map:
                    folder_prefix = self._quest_folder_map[str(_id)]
                    break
        # A caller that already knows which quest FOLDER this turn is about (an attended chat that
        # matched the message to a synced quest) names it directly: same boost, no map needed.
        if folder_prefix is None and meta and meta.get("quest_folder") and self._repo_root:
            try:
                folder_prefix = Path(str(meta["quest_folder"])).resolve().relative_to(
                    self._repo_root).as_posix()
            except (ValueError, OSError):
                folder_prefix = None

        # Candidate pool for the keyword arm. When the repository exposes NATIVE text search
        # (a Qdrant-backed repo, say), let it serve the candidates directly instead of scanning
        # every card in memory; the store still applies its own IDF ranking / confidence gate /
        # recency over that smaller pool below, so selection behavior is unchanged in kind. Detected
        # by duck-typing (never isinstance): a repo without ``search_cards`` (e.g. the default
        # filesystem repo) falls through to the full in-app IDF over ``_load_all()``, byte-for-byte
        # as before. A repo whose ``search_cards`` returns ``None`` also falls back.
        cards = self._repo_text_search_candidates(task_text)
        if cards is None:
            cards = self._load_all()
        if not cards:
            return AssembledContext()

        # Cross-quest fence (see core/scope_tags.py): drop any candidate whose OWN scope_tags are
        # non-empty and disjoint from this turn's scope_tags, before scoring/the LLM filter ever see
        # it -- covers BOTH candidate-loading paths above (native search_cards and the in-app
        # _load_all() scan) in one place. Untagged cards and turns with no scope_tags are unaffected.
        turn_scope_tags = (meta or {}).get("scope_tags")
        # And never the run's own thread: a card recorded from this thread's earlier runs is its
        # previous brief, already in front of the run through its own history.
        own_task = str((meta or {}).get("task_id") or "")
        cards = {
            cid: c for cid, c in cards.items()
            if scope_tags_allow(c.get("scope_tags"), turn_scope_tags)
            and not (own_task and str(((c.get("provenance") or {}) if isinstance(
                c.get("provenance"), dict) else {}).get("task_id") or "") == own_task)
        }
        # PRIORITY CARDS: ids the caller already knows this turn needs (the matched quest's card,
        # or a task's explicitly attached cards). Always selected, first, past the confidence gate
        # and the LLM filter; still subject to the scope fence above. Read through the repository
        # when the candidate pool (a native-search subset) does not hold them.
        priority_cards: List[Dict[str, Any]] = []
        for pid in (meta or {}).get("priority_card_ids") or []:
            pcard = cards.get(str(pid))
            if pcard is None:
                try:
                    pcard = self._repo.read(str(pid))
                except Exception:  # noqa: BLE001
                    pcard = None
                if not isinstance(pcard, dict) or not scope_tags_allow(
                        pcard.get("scope_tags"), turn_scope_tags):
                    pcard = None
            if pcard is not None and all(pcard is not c for c in priority_cards):
                priority_cards.append(pcard)
        if not cards and not priority_cards:
            return AssembledContext()

        # ---- Field-weighted TF-IDF scoring ----
        # Each card has a term->weight map (keywords=3, summary+filename=2,
        # symbols=1, dir components=0.5, extensions dropped). DF is computed
        # from presence (a term counts once per card regardless of weight) so
        # rare terms still get high IDF. Score = sum(field_weight * IDF).
        # A turn that gave up on this search at its soft deadline is no longer waiting for the index:
        # past that moment the build paces itself (see ``Pacer``). Until then it runs at full speed.
        deadline = (meta or {}).get("assembly_deadline")
        pace = Pacer(start_after=float(deadline)) if isinstance(deadline, (int, float)) else None
        card_weight_maps, df = self._scoring_index(cards, pace)
        N = len(cards)

        # IDF(term) = log((N+1)/(df+1)) + 1  (smooth, always >= 1).
        def _idf(term: str) -> float:
            return math.log((N + 1) / (df.get(term, 0) + 1)) + 1.0

        # Score each card: sum of field_weight * IDF for each query term present.
        # Test-file cards are down-weighted by their stored ``weight`` (default 0.5).
        # Tie-break by (usage_count DESC, last_verified_at DESC).
        scored: List[tuple] = []  # (-score, -usage_count, -last_verified_ts, card_dict)
        idf_score_map: Dict[str, float] = {}  # card_id -> raw score (for relevance display)
        for cid, card in cards.items():
            tw = card_weight_maps[cid]
            base_score = sum(tw[t] * _idf(t) for t in task_kws if t in tw)
            # Apply test-file penalty.
            card_weight = float(card.get("weight", _SOURCE_FILE_WEIGHT))
            score = base_score * card_weight
            # QUEST-FOLDER BOOST (pre-gate; see _QUEST_FOLDER_BOOST for why this differs from the
            # post-gate recency/usage boost below).
            if folder_prefix and self._card_pins_folder(card, folder_prefix):
                score *= _QUEST_FOLDER_BOOST
            # CONFIDENCE GATE: only a match that clears the threshold is injected. A weak match
            # contributes NOTHING, so an uncertain query yields an empty context view and the run
            # falls back to plain Claude Code (never worse). This is what makes the layer dominate:
            # it adds a grounding only when confident, and otherwise equals the baseline.
            if score >= self._confidence_threshold:
                idf_score_map[cid] = score
                usage = card.get("usage_count", 0)
                verified_at = card.get("provenance", {}).get("last_verified_at", "") or ""
                # Gate on the un-boosted relevance score above; rank by a recency/usage-boosted
                # score so a card the user just relied on wins a near-tie (bounded, never resurrects
                # an irrelevant card because the gate already passed on relevance alone).
                rank_score = score * self._recency_boost_factor(card)
                scored.append((-rank_score, -usage, -len(verified_at), verified_at, card))

        if not scored and not priority_cards:
            # OPTIONAL gate: a consumer that already knows (via its own reach judge or
            # classifier) that this turn is not about this corpus at all can set
            # ``meta["skip_corpus_fallback"] = True`` to skip the grep fallback entirely instead
            # of merely shrinking it. This is a STRUCTURED signal the consumer computed, never a
            # keyword scan of anything here (hard rule #3): the library only reads the flag, it
            # never guesses at it. Absent/false keeps today's behavior (the capped fallback runs).
            if (meta or {}).get("skip_corpus_fallback"):
                return AssembledContext()
            return self._fallback_file_search(task_kws)

        # Sort: primary descending score, then tie-break descending usage_count,
        # then tie-break by presence of a verified_at string (longer = more recent).
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        # Take up to 2x max_cards as IDF candidates so the LLM filter has enough to work with.
        idf_candidates = [x[4] for x in scored[: self._max_cards * 2]]

        # ---- LLM relevance filter (when a provider is wired) ----
        # IDF finds cards that share keywords with the task.  The LLM filter culls
        # cards that share keywords but are semantically unrelated to the task.
        # Also ranks the files within each kept card by relevance and returns only
        # the relevant ones.
        llm_meta_map: Dict[str, Any] = {}  # card_id -> CardMetadata from LLM filter
        if self._provider is not None and idf_candidates:
            try:
                from ..core.card_filter import filter_cards_by_relevance
                # The filter judges a card by its TITLE and the FILES it covers. A learned
                # reference card has neither a bootstrapped ``summary`` nor ``files`` entries: its
                # title is its ``name``/``description`` and the files it covers are its file
                # REFERENCES. Feeding those in is what stops the filter from seeing an untitled,
                # zero-file card and culling the very card that carries the run's hard-won paths.
                candidate_dicts = [
                    {
                        "id": c.get("id", ""),
                        "title": _card_display_title(c),
                        "files": _card_covered_paths(c),
                        "adapter": "keyword",
                    }
                    for c in idf_candidates
                ]
                filtered = filter_cards_by_relevance(
                    task_text, candidate_dicts, model_provider=self._provider,
                    model=self._filter_model,
                )
                for cm in filtered:
                    llm_meta_map[cm.id] = cm
                # Keep only LLM-approved cards, in LLM relevance order.
                idf_candidates = [
                    c for c in idf_candidates if c.get("id", "") in llm_meta_map
                ]
                idf_candidates.sort(
                    key=lambda c: llm_meta_map[c.get("id", "")].relevance_score,
                    reverse=True,
                )
            except Exception:  # noqa: BLE001
                _log.debug("LLM card filter failed, using IDF ranking", exc_info=True)

        top_cards = idf_candidates[: self._max_cards]
        if priority_cards:
            priority_ids = {c.get("id", "") for c in priority_cards}
            rest = [c for c in top_cards if c.get("id", "") not in priority_ids]
            top_cards = priority_cards + rest[: max(0, self._max_cards - len(priority_cards))]

        # QUERY-AWARE TIME FILTER (spec v3 work package C, item level): when the caller's meta
        # carries a ``time_range`` (the shape ``parse_goal_condition_reply`` emits, threaded in by
        # the orchestrator as ``meta["time_range"]``), apply it as a HARD filter over each selected
        # card's CONTENT ITEMS by their ``ts``: an item carrying a real timestamp outside the range
        # is dropped before rendering, while an item with NO timestamp is always kept (absence of a
        # timestamp must never hide content). A card whose items are ALL filtered out is dropped
        # from the result entirely; when the filter would empty EVERY selected card, it degrades to
        # the unfiltered selection with an explicit note line (never a silent empty). Filtered
        # cards are shallow COPIES so the in-memory cache's card dicts are never mutated. Absent
        # meta / no ``time_range`` key: byte-for-byte today's behavior.
        time_filter_note = ""
        time_range = (meta or {}).get("time_range")
        if time_range and top_cards:
            time_filtered_cards: List[Dict[str, Any]] = []
            for card in top_cards:
                content = _normalize_content(card.get("content"))
                if not content:
                    # No dated content to filter on (a file-only card): keep it as-is.
                    time_filtered_cards.append(card)
                    continue
                kept_items = filter_content_by_time_range(content, time_range)
                if not kept_items:
                    continue  # every item fell outside the range: drop the whole card
                if len(kept_items) != len(content):
                    card = {**card, "content": kept_items}
                time_filtered_cards.append(card)
            if time_filtered_cards:
                top_cards = time_filtered_cards
            else:
                time_filter_note = (
                    "(Note: no card content matched the requested time range. "
                    "Showing context without the time filter.)"
                )

        # Render each card, checking file freshness.
        # Collect every (card_index, file_entry) pair that needs a fingerprint check, then
        # compute all sha256 reads concurrently -- sha256 I/O releases the GIL so threads
        # help whenever a card set pins many files.  Order is preserved; a failed
        # fingerprint yields an empty dict (same as today, never raises).
        #
        # Build the flat list of (card_idx, fe) pairs to fingerprint.
        # Files are sorted by mtime descending (most recently modified first) before
        # fingerprinting so both the freshness check and the rendered view order matches
        # the user expectation of "most recent sources first".
        fp_jobs: List[tuple] = []  # (card_idx, fe_idx, fpath)
        for ci, card in enumerate(top_cards):
            sorted_files = sorted(
                card.get("files", []),
                key=lambda fe: fe.get("mtime", 0.0),
                reverse=True,
            )
            card["_sorted_files"] = sorted_files  # stash for render phase below
            for fi, fe in enumerate(sorted_files):
                fp_jobs.append((ci, fi, fe))

        # Dispatch freshness checks in parallel (stat-first, no git: see _current_sha).
        fp_results: Dict[tuple, Dict[str, Any]] = {}  # (ci, fi) -> {"sha256": ...}
        if fp_jobs:
            n_workers = min(8, len(fp_jobs))
            raw = _run_parallel(
                [lambda ci=ci, fi=fi, fe=fe: (
                    (ci, fi), {"sha256": self._current_sha(fe.get("path", ""), fe)})
                 for ci, fi, fe in fp_jobs],
                max_workers=n_workers,
            )
            for item in raw:
                if item is not None:
                    key, fp = item
                    fp_results[key] = fp or {}

        view_parts: List[str] = []
        card_ids: List[str] = []
        stale_list: List[str] = []

        for ci, card in enumerate(top_cards):
            card_id = card.get("id", "")
            card_ids.append(card_id)
            # A learned card has no ``summary``: its title is its name/description (see
            # _card_display_title). Without this it rendered as "(no summary)" and the worker was
            # handed a headless block of references.
            summary = _card_display_title(card) or "(no summary)"
            # Use the pre-sorted file list (mtime descending = most recently modified first).
            files = card.get("_sorted_files", card.get("files", []))

            # When the LLM filter ran, restrict displayed files to the LLM-ranked set.
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and llm_cm.files:
                llm_path_set = set(llm_cm.files)
                # Keep LLM-selected files in mtime order; then append remaining files
                # so nothing is dropped from the staleness check.
                llm_files = [fe for fe in files if fe.get("path", "") in llm_path_set]
                other_files = [fe for fe in files if fe.get("path", "") not in llm_path_set]
                files = llm_files + other_files

            file_lines: List[str] = []
            for fi, fe in enumerate(files):
                fpath = fe.get("path", "")
                stored_sha = fe.get("sha256", "")
                current_fp = fp_results.get((ci, fi), {})
                current_sha = current_fp.get("sha256", "")
                changed = (
                    (bool(stored_sha) and bool(current_sha) and current_sha != stored_sha)
                    or (bool(stored_sha) and not current_sha)  # file disappeared
                )
                if changed:
                    stale_list.append(fpath)
                    file_lines.append(f"  - {fpath} (changed since last capture)")
                else:
                    why = fe.get("why", "")
                    syms = fe.get("symbols", [])
                    sym_note = f" [{', '.join(syms[:5])}]" if syms else ""
                    file_lines.append(
                        f"  - {fpath}{sym_note}" + (f"  -- {why}" if why else "")
                    )

            # Source-agnostic CONTENT (references resolved fresh + LLM notes), recency-bounded.
            content_lines = self._render_card_content(card, task_kws)

            file_block = (
                "\n".join(file_lines) if file_lines
                else ("  (no pinned files)" if not content_lines else "")
            )
            conventions = card.get("conventions", [])
            conv_block = (
                "\n".join(f"  * {c}" for c in conventions[:10]) if conventions else ""
            )
            part = f"### Card: {card_id}\n{summary}"
            if file_lines or not content_lines:
                part += f"\n\nFiles:\n{file_block}"
            if content_lines:
                part += "\n\nContent:\n" + "\n".join(content_lines)
            if conv_block:
                part += f"\n\nConventions:\n{conv_block}"

            # Surface other source files in the same directories that weren't sampled
            # into this card during bootstrap, so the context engine can decide whether
            # to pull them in.
            if self._repo_root is not None:
                pinned_paths: Set[str] = {fe.get("path", "") for fe in card.get("files", [])}
                dirs_to_scan: Set[str] = set()
                for fp in pinned_paths:
                    if fp:
                        parent = str(Path(fp).parent)
                        dirs_to_scan.add(parent)
                sibling_paths: List[str] = []
                for d in sorted(dirs_to_scan):
                    dir_abs = self._repo_root / d
                    if dir_abs.is_dir():
                        try:
                            for entry in sorted(dir_abs.iterdir()):
                                if (entry.is_file()
                                        and entry.suffix in _SOURCE_EXTS
                                        and not entry.name.startswith(".")):
                                    rel = str(entry.relative_to(self._repo_root))
                                    if rel not in pinned_paths:
                                        sibling_paths.append(rel)
                        except OSError:
                            pass
                if sibling_paths:
                    shown = sibling_paths[:12]
                    more = len(sibling_paths) - len(shown)
                    sibling_lines = "\n".join(f"  - {p}" for p in shown)
                    if more:
                        sibling_lines += f"\n  - … and {more} more"
                    part += f"\n\nOther files in the same directories (not sampled into this card):\n{sibling_lines}"

            view_parts.append(part)

        context_view = "\n\n---\n\n".join(view_parts)
        if time_filter_note:
            # Time-filter degrade (see above): everything the filter touched was emptied, so the
            # UNFILTERED selection renders with an explicit note leading it, never a silent empty.
            context_view = (
                time_filter_note + "\n\n" + context_view if context_view else time_filter_note
            )

        # --- Context transparency: collect the file paths surfaced by this arm ----------------
        # One source entry per arm (keyword/IDF), listing the pinned file paths (mtime-sorted,
        # LLM-filtered when available) so the orchestrator can emit them for the user.
        _source_items: List[str] = []
        for card in top_cards:
            card_id = card.get("id", "")
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and llm_cm.files:
                # LLM-ranked files only, in LLM relevance order
                for fp in llm_cm.files:
                    if fp and fp not in _source_items:
                        _source_items.append(fp)
            else:
                for fe in card.get("_sorted_files", card.get("files", [])):
                    fp = fe.get("path", "")
                    if fp and fp not in _source_items:
                        _source_items.append(fp)
        _sources = (
            [{"adapter": "keyword", "label": "docstring cards", "items": _source_items}]
            if _source_items else []
        )

        # --- Card metadata: populate selection info for UI display and transparency ---------
        # Build metadata for each selected card so the orchestrator can emit which cards were chosen.
        # Use the LLM-judged relevance score when available; otherwise use the normalized IDF score.
        _max_idf = max(idf_score_map.values()) if idf_score_map else 1.0
        card_metadata: List[Dict[str, Any]] = []
        for ci, card in enumerate(top_cards):
            card_id = card.get("id", "")
            llm_cm = llm_meta_map.get(card_id)
            # LLM-ranked files (already relevance-ordered), else mtime-sorted files
            if llm_cm is not None and llm_cm.files:
                display_files = llm_cm.files[:3]
                relevance_score = llm_cm.relevance_score
            else:
                # Pinned files first, then the card's file REFERENCES: a learned card pins no
                # files, so without the references it would report covering nothing.
                display_files = _card_covered_paths(card, limit=3)
                relevance_score = idf_score_map.get(card_id, 0.0) / _max_idf
            # Structured content ITEMS (id/type/why/locator/text/preview/pointer_eligible) resolved
            # FRESH, the same blocks that fed this card's view lines. The consolidating filter selects
            # from these, and the deep preamble materializes paste-vs-pointer from their locators.
            item_blocks = render_card_content_blocks(
                card,
                self._resolvers,
                task_kws=task_kws,
                max_refs=self._max_card_refs,
                max_ref_chars=self._max_card_ref_chars,
            )
            card_metadata.append({
                "id": card_id,
                "title": _card_display_title(card),
                "relevance_score": relevance_score,
                "file_count": len(card.get("files", [])),
                "files": display_files,
                "adapter": "keyword",
                "items": item_blocks,
                # OPTIONAL card taxonomy fields, passed through verbatim when the card carries them
                # (a card is a plain dict; a consumer is free to type and version its own cards).
                # ``card_type`` lets a consumer tell TOPIC cards apart from its other card kinds
                # (permissions, settings, derived doc cards) when it threads ideas by card, and
                # ``lifecycle`` says whether the work behind a card is still open. Absent on a card
                # that does not use them, so nothing changes for a consumer that never sets them.
                "card_type": card.get("card_type", ""),
                "lifecycle": card.get("lifecycle", ""),
                # The card's OWN scope_tags (see core/scope_tags.py), passed through verbatim so a
                # caller that persists this bundle across turns (the anticipation engine's
                # precompute) can UNION them onto whatever it stores, even when THIS call's own
                # ``meta["scope_tags"]`` was empty. See vector_context_assembler's matching field
                # for why this matters. Empty when the card is untagged.
                "scope_tags": list(card.get("scope_tags") or []),
                # The VERBATIM rendered section this card contributed to context_view (the whole
                # ``### Card: ...`` block: summary + Files listing + Content + Conventions). The hybrid
                # consolidator rebuilds from this so a keyword card's file listings are never lost when
                # consolidation engages (they are NOT content items). top_cards and view_parts are built
                # in lockstep above, so view_parts[ci] is this card's section.
                "rendered_section": view_parts[ci] if ci < len(view_parts) else "",
            })

        # --- PER-SOURCE USAGE RECENCY: bump what was actually USED, not merely what was held ------
        # The card-level ``usage_count`` (record()) only ever said THIS CARD was used. It could not
        # say WHICH of the card's sources carried the value, so a card that accumulated sources had
        # no way to let the dead ones sink. Here, at the seam where sources are actually RESOLVED
        # AND RENDERED into the context view, each source that made it in is stamped with
        # ``last_used_ts`` + ``use_count``. Sources merely listed on a card but not rendered stay
        # cold. Deliberately AFTER the render above: this turn's bytes are computed from the
        # PRE-bump values, so the same card rendered twice in a row is byte-identical (the recency
        # data never enters the rendered text, and every rendered item is re-warmed by the same
        # amount, which cannot reorder them). Best-effort: never raises, never blocks assembly.
        try:
            self._bump_source_usage(top_cards, card_metadata, llm_meta_map)
        except Exception:  # noqa: BLE001 — usage bookkeeping must never break assembly
            _log.debug("per-source usage bump failed", exc_info=True)

        # Clean up the temp sort key we stashed on card dicts (they're in-memory cache copies).
        for card in top_cards:
            card.pop("_sorted_files", None)

        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
            sources=_sources,
            card_metadata=card_metadata,
        )

    def _bump_source_usage(
        self,
        top_cards: List[Dict[str, Any]],
        card_metadata: List[Dict[str, Any]],
        llm_meta_map: Dict[str, Any],
    ) -> None:
        """Stamp per-source usage recency on every source this assembly actually rendered.

        For each selected card:
          * its CONTENT items that RESOLVED into rendered blocks (the ids in ``card_metadata``'s
            ``items``) get ``last_used_ts = now`` and ``use_count += 1``;
          * its FILE entries that were rendered as this task's relevant files get the same. When the
            LLM relevance filter ran, "relevant" is exactly the set it selected; without a filter
            every listed file is rendered, so every listed file counts as used.

        Type-agnostic: a content item is bumped by ID, so a conversation, collection, query, or note
        reference is warmed exactly like a file. Never raises. Writes nothing in dry-run.
        """
        if self._dry_run:
            return
        now = time.time()
        meta_by_id = {cm.get("id"): cm for cm in card_metadata if isinstance(cm, dict)}
        for card in top_cards:
            card_id = card.get("id", "")
            if not card_id:
                continue
            cm = meta_by_id.get(card_id) or {}
            used_item_ids = {
                str(b.get("id")) for b in (cm.get("items") or [])
                if isinstance(b, dict) and b.get("id")
            }
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and getattr(llm_cm, "files", None):
                used_paths = {fp for fp in llm_cm.files if fp}
            else:
                used_paths = {
                    fe.get("path", "")
                    for fe in card.get("_sorted_files", card.get("files", []))
                    if fe.get("path")
                }
            if not used_item_ids and not used_paths:
                continue
            try:
                # wait=False: the stamp is bookkeeping, and a turn never waits for another process's hold on
                # the card folder (a tidy, the card limit, a learned write); a stamp that finds it busy is skipped.
                self._mark_sources_used_inner(card_id, used_item_ids, used_paths, now, wait=False)
            except Exception:  # noqa: BLE001 — one card's bookkeeping never blocks the rest
                _log.debug("usage bump failed for card %s", card_id, exc_info=True)

    def _mark_sources_used_inner(
        self, card_id: str, item_ids: Set[str], file_paths: Set[str], now: float, wait: bool = True
    ) -> bool:
        """Read-modify-write ONE card's per-source usage fields. Returns True when it wrote.

        With ``wait`` off (the assembly path) it takes the card folder's lock only if that is free right now,
        and does nothing when another process or thread holds it: a turn never blocks on a flock.

        Reads the card through the persistence boundary (so it never writes back a stale in-memory
        copy), stamps the used content items and file entries, and persists only when something
        actually changed. A card whose sources predate the fields simply gains them here: no
        migration pass, no rewrite storm (a card is only ever touched when it is USED).
        """
        if self.is_mounted_card(card_id):
            return False  # usage heat on a mounted card would be a write into the owner's store
        lock = self.folder_lock()
        take = getattr(lock, "acquire", None)
        if not wait and take is not None:
            if not take(blocking=False):
                return False
            try:
                return self._mark_sources_used_locked(card_id, item_ids, file_paths, now)
            finally:
                lock.release()
                self.limit_guard.speak()
        with lock:
            written = self._mark_sources_used_locked(card_id, item_ids, file_paths, now)
        self.limit_guard.speak()
        return written

    def _mark_sources_used_locked(
        self, card_id: str, item_ids: Set[str], file_paths: Set[str], now: float
    ) -> bool:
        """The read-modify-write of ``_mark_sources_used_inner``, run while the card folder's lock is held."""
        loaded = self._repo.read(card_id)
        if not isinstance(loaded, dict):
            return False
        card = loaded
        changed = False
        if item_ids:
            content = _normalize_content(card.get("content"))
            if content and _mark_items_used(content, item_ids, now=now,
                                            min_interval=_SOURCE_USAGE_MIN_INTERVAL_SECONDS):
                card["content"] = content
                changed = True
        if file_paths:
            for fe in card.get("files", []) or []:
                if not isinstance(fe, dict) or fe.get("path") not in file_paths:
                    continue
                try:
                    prev = float(fe.get("last_used_ts") or 0.0)
                except (TypeError, ValueError):
                    prev = 0.0
                if (now - prev) < _SOURCE_USAGE_MIN_INTERVAL_SECONDS:
                    continue  # same use, already stamped moments ago (see mark_items_used)
                fe["last_used_ts"] = now
                try:
                    fe["use_count"] = int(fe.get("use_count") or 0) + 1
                except (TypeError, ValueError):
                    fe["use_count"] = 1
                changed = True
        if not changed:
            return False
        self._write_card_atomic(card_id, card)
        return True

    def mark_sources_used(
        self,
        card_id: str,
        *,
        item_ids: Optional[List[str]] = None,
        file_paths: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Public: stamp per-source usage recency on ``card_id``'s named sources. Never raises.

        The assembly path bumps automatically (see ``_bump_source_usage``); this is the seam for any
        OTHER consumer or assembler arm that renders a card's sources into context and wants the
        same heat signal recorded. Returns True when the card was written.
        """
        try:
            return self._mark_sources_used_inner(
                card_id, set(item_ids or []), set(file_paths or []),
                float(now) if now is not None else time.time(),
            )
        except Exception:  # noqa: BLE001
            return False

    def _repo_text_search_candidates(
        self, task_text: str
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Candidate cards from the repository's NATIVE text search, or ``None`` to fall back.

        Returns ``None`` (so the caller scans ``_load_all()`` with in-app IDF, today's behavior)
        when the repository has no native text search OR its search returns ``None`` OR anything
        goes wrong. Returns ``{card_id: card_dict}`` when the repo served a native-search candidate
        pool for the keyword arm. Detected by duck-typing (``hasattr``), never an isinstance check,
        so any repo that exposes a ``search_cards(query, *, limit)`` method participates. Never
        raises.
        """
        search = getattr(self._repo, "search_cards", None)
        if not callable(search):
            return None
        try:
            # Ask for a generous pool: the keyword arm keeps up to ``_max_cards * 2`` IDF
            # candidates, so a higher limit lets the repo's text search supply enough rows for the
            # store's IDF ranking / confidence gate / recency to choose among.
            limit = max(self._max_cards * 4, 32)
            result = search(task_text, limit=limit)
            if result is None:
                return None
            return result if isinstance(result, dict) else None
        except Exception:  # noqa: BLE001 — native search is best-effort; fall back to in-app IDF
            _log.debug("repo search_cards failed; falling back to in-app IDF", exc_info=True)
            return None
