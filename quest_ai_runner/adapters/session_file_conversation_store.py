"""SessionFileConversationStore — a local ConversationStore over Claude session files.

A reference implementation of the ``ConversationStore`` Protocol (``core.adapters``) backed by
local Claude session JSON files (``~/.claude/sessions`` and any ``.claude``/``conversations`` dirs
under a corpus root). The orchestrator's User Input Understanding step uses it to resolve a short or
anaphoric message ("ok do it", "the first one") into a self-contained goal condition.

It reuses the shared conversation load/parse helpers (``conversation_format``) and the TF-DF-IDF
selection heuristic (``tfdfidf_sampling``) so a long conversation is sampled, not dumped. A
different backend (e.g. Mongo) can satisfy the same Protocol separately.

RECALL IS RELEVANCE-FIRST OVER THE FULL HORIZON, AND STAYS CHEAP AS THE HORIZON GROWS
-------------------------------------------------------------------------------------
``related_slices`` never applies a time window or hard recency cutoff: EVERY session file on disk
is a candidate on every call, so an old-but-relevant conversation is always reachable. To keep that
affordable for an ever-growing conversation dir it runs a two-stage scan:

  1. STAGE 1 (cheap, all files): each file gets a compact DIGEST (first/last message snippets)
     cached per file and invalidated by (mtime, size), so after the first pass a call costs one
     ``stat`` per file. All candidates are ranked by TF-DF-IDF relevance of digest vs the query
     (``rank_candidates_by_digest``), with recency only a small score boost, never a filter. A
     small RECENCY FLOOR (the most recent few files) always joins the shortlist regardless of
     match, so "what did we do yesterday?" works even when its words match nothing.
  2. STAGE 2 (bounded): only the shortlisted files (a hard cap, independent of how many
     conversations exist) are fully loaded, scope-filtered, and passed to ``select_related`` for
     the final pick + rendering under ``max_chars``.

The file index is refreshed at most every ``_RESCAN_SECONDS`` so conversations written after
construction become reachable without rescanning directories on every call.

The selection and rendering algorithms are the pure module-level functions
``select_current_slice`` and ``select_related`` in ``conversation_format``, so any
``ConversationStore`` backend can reuse the same algorithm without duplicating it.

Every method NEVER raises — it returns an empty ``ConversationContext`` on any failure.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from quest_ai_runner.core.adapters import ConversationContext, ConversationStore

from .conversation_format import (
    conversation_digest,
    conversation_messages,
    conversation_timestamp,
    is_claude_conversation,
    nl_terms,
    parse_date_bound,
    rank_candidates_by_digest,
    resolve_conv_key,
    scan_conversation_files,
    select_current_slice,
    select_related,
    timestamp_in_range,
)

# How long a directory scan stays fresh before the next call re-lists session files. Listing is
# cheap (no file content is read) but a corpus-root scan can glob many directories, so it is
# throttled rather than repeated on every call. New conversations become reachable within this.
_RESCAN_SECONDS = 30.0
# A file at or under this size is fully parsed to compute its digest (a one-time cost per file
# version, then cached by (mtime, size)). Bigger files get a bounded head+tail raw read instead,
# so a single huge file can never make the stage-1 scan expensive.
_DIGEST_FULL_PARSE_MAX_BYTES = 512 * 1024
# For an oversized file: how many raw bytes to read from the head and from the tail for its digest.
_DIGEST_RAW_BYTES = 4096
# Stage 2 hard cap: at most this many files are ever FULLY loaded per related_slices call,
# independent of how many conversations exist (the I1 bound for the full-horizon scan).
_MAX_FULL_LOADS = 16
# The recency floor: this many most-recent candidates always join the stage-2 shortlist even when
# their digests match nothing, so recall of "just now" conversations never depends on term overlap.
_RECENCY_FLOOR_CONVS = 2
# How many parsed conversations to keep in the in-memory LRU (validated by (mtime, size)).
_CONV_CACHE_SIZE = 16


class SessionFileConversationStore(ConversationStore):
    """ConversationStore over local Claude session files. Never raises from its public methods."""

    def __init__(self, corpus_root: Optional[str] = None, sessions_dir: Optional[str] = None):
        """Initialize from a corpus root and/or an explicit sessions directory.

        Mirrors ``ClaudeConversationsAdapter``: ``corpus_root`` is scanned recursively for
        ``.claude``/``conversations`` dirs; ``sessions_dir`` defaults to ``~/.claude/sessions``.
        Only file PATHS are indexed here — no conversation content is read until a method needs it.
        """
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self.sessions_dir = (
            Path(sessions_dir) if sessions_dir else Path.home() / ".claude" / "sessions"
        )
        self._filepaths: Dict[str, Path] = {}
        self._conv_id_lookup: Dict[str, str] = {}
        self._scanned_at: float = 0.0
        # Per-file digest cache: {key: {"mtime", "size", "digest", "ts", "valid"}}.
        self._digest_cache: Dict[str, Dict[str, Any]] = {}
        # Small LRU of fully parsed conversations: {key: (mtime, size, conv_dict)}.
        self._conv_cache: "OrderedDict[str, Tuple[float, int, Dict[str, Any]]]" = OrderedDict()
        self._refresh_index(force=True)

    # --- file index + lazy loading ---------------------------------------------

    def _refresh_index(self, *, force: bool = False) -> None:
        """Re-list session files (paths only) at most every ``_RESCAN_SECONDS``. Never raises."""
        now = time.monotonic()
        if not force and (now - self._scanned_at) < _RESCAN_SECONDS:
            return
        try:
            self._filepaths, self._conv_id_lookup = scan_conversation_files(
                self.corpus_root, self.sessions_dir
            )
            self._scanned_at = now
        except Exception:  # noqa: BLE001 — scanning must never raise
            pass

    def _load_conv(self, key: str) -> Optional[Dict[str, Any]]:
        """Fully parse ONE conversation file, LRU-cached and invalidated by (mtime, size).

        Returns None for a missing, unreadable, or non-conversation file. Never raises.
        """
        path = self._filepaths.get(key)
        if path is None:
            return None
        try:
            stat = path.stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            return None
        cached = self._conv_cache.get(key)
        if cached is not None and cached[0] == mtime and cached[1] == size:
            self._conv_cache.move_to_end(key)
            return cached[2]
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not is_claude_conversation(data):
            return None
        self._conv_cache[key] = (mtime, size, data)
        self._conv_cache.move_to_end(key)
        while len(self._conv_cache) > _CONV_CACHE_SIZE:
            self._conv_cache.popitem(last=False)
        return data

    def _digest_entry(self, key: str) -> Optional[Dict[str, Any]]:
        """The cached stage-1 digest entry for one file: {digest, ts, valid}.

        Recomputed only when the file's (mtime, size) changed; otherwise one ``stat`` call. A small
        file is fully parsed (and validated); an oversized file gets a bounded head+tail raw read
        with a cheap conversation-shape check. ``ts`` is the conversation's own timestamp when it
        records one, else the file mtime (recency is only ever a tie-break downstream). Never raises.
        """
        path = self._filepaths.get(key)
        if path is None:
            return None
        try:
            stat = path.stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            return None
        cached = self._digest_cache.get(key)
        if cached is not None and cached["mtime"] == mtime and cached["size"] == size:
            return cached
        entry: Dict[str, Any] = {"mtime": mtime, "size": size, "digest": "", "ts": mtime,
                                 "valid": False}
        try:
            if size <= _DIGEST_FULL_PARSE_MAX_BYTES:
                conv = self._load_conv(key)
                if conv is not None:
                    entry["digest"] = conversation_digest(conv)
                    entry["ts"] = conversation_timestamp(conv) or mtime
                    entry["valid"] = True
            else:
                with open(path, "rb") as f:
                    head = f.read(_DIGEST_RAW_BYTES)
                    tail = b""
                    if size > 2 * _DIGEST_RAW_BYTES:
                        f.seek(-_DIGEST_RAW_BYTES, 2)
                        tail = f.read(_DIGEST_RAW_BYTES)
                head_text = head.decode("utf-8", errors="ignore")
                tail_text = tail.decode("utf-8", errors="ignore")
                if '"messages"' in head_text or '"turns"' in head_text:
                    entry["digest"] = f"START: {head_text} RECENT: {tail_text}"
                    entry["valid"] = True
        except Exception:  # noqa: BLE001 — digest computation must never raise
            pass
        self._digest_cache[key] = entry
        return entry

    # --- current conversation -------------------------------------------------

    def current_slice(self, conv_id: str, query: str, *, recent_turns: int = 4,
                      max_chars: int = 6000,
                      filters: Optional[Dict[str, Any]] = None) -> ConversationContext:
        """Relevant slice of the CURRENT conversation.

        ``filters`` is accepted for ``ConversationStore`` protocol compatibility but not
        meaningfully applicable at single-conversation granularity (there is only ever one
        conversation to filter here) -- it is ignored. A cross-conversation filter belongs in
        ``related_slices`` below.

        The ONLY thing forced into the output is the LAST USER turn (the actual intent). Everything
        else is a CANDIDATE selected by relevance, not auto-included by recency:

          * ``recent_turns`` is the "considered window" (default 4): the last N turns are added to
            the relevance candidate pool, NOT guaranteed in. Older turns are candidates too.
          * USER turns are PREFERRED (a x1.5 score boost in select_representatives) and render
            VERBATIM (capped only if absurdly long). AI turns earn inclusion purely by relevance,
            even the latest one, and are always COMPACTED via ``compact_message``.
          * Per-turn relevance is LENGTH-NORMALIZED so length alone never wins, and the TF-DF-IDF
            *document* for an AI turn is its COMPACT form so a long AI answer cannot dominate df/idf.

        The last USER turn is GUARANTEED present even after ``max_chars`` truncation (if there is no
        user turn, the last message is the fallback anchor). Respects ``max_chars`` (drops the
        oldest non-anchor lines first, sets ``truncated``). Never raises.

        Delegates to the pure ``select_current_slice`` function in ``conversation_format``. Only the
        ONE named conversation file is loaded (lazily, cached), never the whole directory.
        """
        try:
            self._refresh_index()
            key = resolve_conv_key(conv_id, self._filepaths, self._conv_id_lookup)
            if key is None:
                return ConversationContext(scanned=0)
            conv = self._load_conv(key)
            if conv is None:
                return ConversationContext(scanned=0)
            messages = conversation_messages(conv)
            scanned = len(messages)
            if not messages:
                return ConversationContext(scanned=0)

            text, turns_meta, truncated = select_current_slice(
                messages, query, recent_turns=recent_turns, max_chars=max_chars
            )
            sources = [{"conv_id": conv_id, "label": "current conversation"}]
            return ConversationContext(text=text, turns=turns_meta, sources=sources,
                                       scanned=scanned, truncated=truncated)
        except Exception:  # noqa: BLE001 — never raise
            return ConversationContext(scanned=0)

    # --- related conversations ------------------------------------------------

    def related_slices(self, query: str, scope: Dict[str, Any], *,
                       exclude_conv_id: Optional[str] = None, max_convs: int = 3,
                       max_chars: int = 6000,
                       filters: Optional[Dict[str, Any]] = None) -> ConversationContext:
        """TF-DF-IDF-selected slices from OTHER conversations within ``scope``.

        Full-horizon and relevance-first: EVERY session file is a stage-1 candidate on every call
        (no time window, no recency cutoff), ranked by digest relevance with recency only a small
        boost, plus a small most-recent floor. Only the bounded stage-2 shortlist is fully loaded,
        then best-effort scope-filtered (local sessions may lack user_id/team_ids, so a missing
        field is NOT a filter) and rendered as a short slice per conversation under ``max_chars``.
        ``scanned`` reports the stage-1 candidate count (the true horizon considered). Never raises.

        ``filters`` (optional, query-aware retrieval routing): ``{time_range: {start, end},
        topic_terms: [...], content_kind, actor}`` (any subset; unrecognized keys ignored).
        ``time_range`` is a HARD filter applied to stage-1 candidates BEFORE the relevance gate
        above, using each file's cached digest timestamp -- routing time+entity filters first,
        semantic/lexical WITHIN the filtered set. ``topic_terms`` are folded into the query used
        for matching/ranking. ``content_kind``/``actor`` have no reliable structural analogue in
        local session files (a chat transcript has no "kind"; a Mongo-backed store with real
        metadata, e.g. ``MongoConversationStore``, can filter on them) so they are accepted but not
        enforced here. When the time-filtered candidate set is EMPTY, this DEGRADES to today's
        relevance-only behavior over the UNFILTERED candidates (never a silent empty result) and
        sets ``ConversationContext.degraded_note`` plus a labeled line at the top of ``text``.
        """
        try:
            scope = scope or {}
            filters = filters or {}
            self._refresh_index()
            exclude_key = (
                resolve_conv_key(exclude_conv_id, self._filepaths, self._conv_id_lookup)
                if exclude_conv_id else None
            )

            # STAGE 1: every file is a candidate; drop only the excluded conversation and files
            # whose cached digest says they are not conversations at all.
            candidates: Dict[str, Dict[str, Any]] = {}
            for key in list(self._filepaths.keys()):
                if exclude_key is not None and key == exclude_key:
                    continue
                entry = self._digest_entry(key)
                if entry is None or not entry["valid"]:
                    continue
                candidates[key] = entry

            scanned = len(candidates)
            if not candidates:
                return ConversationContext(scanned=0)

            # C2/C3: a HARD time_range filter narrows the stage-1 candidate set itself (routing
            # time filters FIRST), before any relevance ranking. Degrades to the unfiltered set
            # when nothing survives, so an over-narrow filter never reads as an empty history.
            degraded_note: Optional[str] = None
            time_range = filters.get("time_range")
            if isinstance(time_range, dict) and (time_range.get("start") or time_range.get("end")):
                start_epoch = parse_date_bound(time_range.get("start"), end_of_day=False)
                end_epoch = parse_date_bound(time_range.get("end"), end_of_day=True)
                if start_epoch is not None or end_epoch is not None:
                    time_filtered = {
                        k: v for k, v in candidates.items()
                        if timestamp_in_range(float(v["ts"] or 0.0), start_epoch, end_epoch)
                    }
                    if time_filtered:
                        candidates = time_filtered
                    else:
                        degraded_note = (
                            "No conversations found in the specified time range; showing "
                            "broader relevance-based results instead.")

            effective_query = query or ""
            topic_terms = filters.get("topic_terms")
            if isinstance(topic_terms, list) and topic_terms:
                effective_query = (effective_query + " "
                                   + " ".join(str(t) for t in topic_terms)).strip()

            # PRECISION (I4): only candidates whose digest shares at least one term with the query
            # compete on relevance. A query that matches nothing gets ONLY the recency floor below,
            # never a prompt full of unrelated conversations. An empty/blank query keeps everyone
            # (pure distinctiveness + recency ranking, the pre-query behavior).
            query_terms = nl_terms(effective_query)
            if query_terms:
                matched = [k for k in candidates
                           if nl_terms(candidates[k]["digest"]) & query_terms]
            else:
                matched = list(candidates.keys())

            shortlist_n = min(max(4 * max(1, int(max_convs)), 8), _MAX_FULL_LOADS)
            shortlist = rank_candidates_by_digest(
                matched,
                digest_of=lambda k: candidates[k]["digest"],
                timestamp_of=lambda k: float(candidates[k]["ts"] or 0.0),
                query=effective_query,
                top_n=shortlist_n,
            )
            # Recency floor: the most recent few candidates always join (and always render, via
            # must_include below) regardless of match, so "what did we just do?" works even when
            # its words overlap nothing.
            by_recency = sorted(candidates, key=lambda k: float(candidates[k]["ts"] or 0.0),
                                reverse=True)
            floor_keys = by_recency[:_RECENCY_FLOOR_CONVS]
            for k in floor_keys:
                if k not in shortlist:
                    shortlist.append(k)
            shortlist = shortlist[:_MAX_FULL_LOADS]

            # STAGE 2: fully load ONLY the shortlist, scope-filter, and hand to select_related
            # (which re-ranks precisely and renders under max_chars). The original store key is
            # injected under "id" so the "=== Related conversation: {key} ===" header keeps its
            # format.
            conv_list: List[Dict[str, Any]] = []
            for key in shortlist:
                conv = self._load_conv(key)
                if conv is None:
                    continue
                if not self._scope_matches(conv, scope):
                    continue
                conv = dict(conv)
                conv["id"] = key
                conv_list.append(conv)

            if not conv_list:
                return ConversationContext(scanned=scanned, degraded_note=degraded_note)

            text, sources, truncated = select_related(
                conv_list, effective_query, max_convs=max_convs, max_chars=max_chars,
                must_include_ids={k for k in floor_keys},
            )
            if degraded_note:
                text = f"(Note: {degraded_note})\n\n{text}" if text else f"(Note: {degraded_note})"
            return ConversationContext(text=text, sources=sources, scanned=scanned,
                                       truncated=truncated, degraded_note=degraded_note)
        except Exception:  # noqa: BLE001 — never raise
            return ConversationContext(scanned=0)

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _scope_matches(conv: Any, scope: Dict[str, Any]) -> bool:
        """Best-effort scope filter: a scope field constrains only when BOTH it and the matching
        conversation field are present. A missing field on either side is not a filter (local
        session files often lack user_id/team_ids)."""
        if not isinstance(conv, dict) or not scope:
            return True
        # user_id: exact match when both present.
        want_user = scope.get("user_id")
        if want_user is not None and conv.get("user_id") is not None:
            if str(conv.get("user_id")) != str(want_user):
                return False
        # team_ids: overlap when both present.
        want_teams = scope.get("team_ids")
        conv_teams = conv.get("team_ids")
        if want_teams and conv_teams:
            try:
                if not (set(map(str, want_teams)) & set(map(str, conv_teams))):
                    return False
            except TypeError:
                pass
        # since: keep conversations at/after the timestamp when both present.
        since = scope.get("since")
        if isinstance(since, (int, float)):
            ts = conversation_timestamp(conv)
            if ts and ts < float(since):
                return False
        # participant_id: membership when both present.
        want_participant = scope.get("participant_id")
        participants = conv.get("participant_ids") or conv.get("participants")
        if want_participant is not None and participants:
            try:
                if str(want_participant) not in set(map(str, participants)):
                    return False
            except TypeError:
                pass
        return True
