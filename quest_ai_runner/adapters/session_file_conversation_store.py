"""SessionFileConversationStore — a local ConversationStore over Claude session files.

A reference implementation of the ``ConversationStore`` Protocol (``core.adapters``) backed by
local Claude session JSON files (``~/.claude/sessions`` and any ``.claude``/``conversations`` dirs
under a corpus root). The orchestrator's User Input Understanding step uses it to resolve a short or
anaphoric message ("ok do it", "the first one") into a self-contained goal condition.

It reuses the shared conversation load/parse helpers (``conversation_format``) and the TF-DF-IDF
selection heuristic (``tfdfidf_sampling``) so a long conversation is sampled, not dumped. A
different backend (e.g. Mongo) can satisfy the same Protocol separately.

The selection and rendering algorithms are extracted into the pure module-level functions
``select_current_slice`` and ``select_related`` in ``conversation_format``, so any
``ConversationStore`` backend can reuse the same algorithm without duplicating it. The store
methods delegate to them after handling file-loading and scope-filtering.

Every method NEVER raises — it returns an empty ``ConversationContext`` on any failure.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import ConversationContext, ConversationStore

from .conversation_format import (
    conversation_messages,
    load_conversations,
    resolve_conv_key,
    select_current_slice,
    select_related,
)


class SessionFileConversationStore(ConversationStore):
    """ConversationStore over local Claude session files. Never raises from its public methods."""

    def __init__(self, corpus_root: Optional[str] = None, sessions_dir: Optional[str] = None):
        """Initialize from a corpus root and/or an explicit sessions directory.

        Mirrors ``ClaudeConversationsAdapter``: ``corpus_root`` is scanned recursively for
        ``.claude``/``conversations`` dirs; ``sessions_dir`` defaults to ``~/.claude/sessions``.
        """
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self.sessions_dir = (
            Path(sessions_dir) if sessions_dir else Path.home() / ".claude" / "sessions"
        )
        self._conversations: Dict[str, Any] = {}
        self._filepaths: Dict[str, Path] = {}
        self._conv_id_lookup: Dict[str, str] = {}
        try:
            self._conversations, self._filepaths, self._conv_id_lookup = load_conversations(
                self.corpus_root, self.sessions_dir
            )
        except Exception:  # noqa: BLE001 — load must never raise from construction
            pass

    # --- current conversation -------------------------------------------------

    def current_slice(self, conv_id: str, query: str, *, recent_turns: int = 4,
                      max_chars: int = 6000) -> ConversationContext:
        """Relevant slice of the CURRENT conversation.

        The ONLY thing forced into the output is the LAST USER turn (the actual intent). Everything
        else is a CANDIDATE selected by relevance, not auto-included by recency:

          * ``recent_turns`` is now the "considered window" (default 4): the last N turns are added to
            the relevance candidate pool, NOT guaranteed in. Older turns are candidates too.
          * USER turns are PREFERRED (a x1.5 score boost in select_representatives) and render
            VERBATIM (capped only if absurdly long). AI turns earn inclusion purely by relevance,
            even the latest one, and are always COMPACTED via ``compact_message``.
          * Per-turn relevance is LENGTH-NORMALIZED so length alone never wins, and the TF-DF-IDF
            *document* for an AI turn is its COMPACT form so a long AI answer cannot dominate df/idf.

        The last USER turn is GUARANTEED present even after ``max_chars`` truncation (if there is no
        user turn, the last message is the fallback anchor). Respects ``max_chars`` (drops the
        oldest non-anchor lines first, sets ``truncated``). Never raises.

        Delegates to the pure ``select_current_slice`` function in ``conversation_format``.
        """
        try:
            key = resolve_conv_key(conv_id, self._conversations, self._conv_id_lookup)
            if key is None:
                return ConversationContext(scanned=0)
            messages = conversation_messages(self._conversations[key])
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
                       max_chars: int = 6000) -> ConversationContext:
        """TF-DF-IDF-selected slices from OTHER conversations within ``scope``. Best-effort filters
        the conversation dicts by ``scope`` fields that are actually present (local sessions may lack
        user_id/team_ids, so a missing field is NOT a filter). Renders a short slice per
        conversation. Respects ``max_chars``. Never raises.

        Delegates to the pure ``select_related`` function in ``conversation_format`` after applying
        scope filtering here (scope matching is local-session-specific logic).
        """
        try:
            scope = scope or {}
            exclude_key = (
                resolve_conv_key(exclude_conv_id, self._conversations, self._conv_id_lookup)
                if exclude_conv_id else None
            )
            # Build the candidate list and preserve their original dict keys for the header format.
            candidate_keys: List[str] = []
            for key, conv in self._conversations.items():
                if exclude_key is not None and key == exclude_key:
                    continue
                if not self._scope_matches(conv, scope):
                    continue
                candidate_keys.append(key)

            scanned = len(candidate_keys)
            if not candidate_keys:
                return ConversationContext(scanned=0)

            # Build a list of conversation dicts with their original store key injected under "id"
            # so select_related can render the same "=== Related conversation: {key} ===" header.
            conv_list = []
            for key in candidate_keys:
                conv = dict(self._conversations[key])
                conv["id"] = key
                conv_list.append(conv)

            text, sources, truncated = select_related(
                conv_list, query, max_convs=max_convs, max_chars=max_chars
            )
            return ConversationContext(text=text, sources=sources, scanned=scanned,
                                       truncated=truncated)
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
        from .conversation_format import conversation_timestamp
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
