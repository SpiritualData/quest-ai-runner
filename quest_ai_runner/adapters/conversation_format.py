"""Shared Claude-session conversation loading + parsing helpers.

Factored out of ``claude_conversations_adapter`` so BOTH the ``ClaudeConversationsAdapter``
(a RetrievalAdapter) and the ``SessionFileConversationStore`` (a ConversationStore) read and
render local Claude session files the SAME way, with no duplicated logic.

Everything here is stdlib-only and best-effort: a malformed or unreadable file is skipped, never
fatal. A "conversation" is a JSON object with a ``messages`` (or ``turns``) array of
``{role, text|content}`` message dicts.

PURE SELECTION FUNCTIONS
------------------------
``select_current_slice`` and ``select_related`` implement the TF-DF-IDF-based selection and
rendering algorithms used by ``SessionFileConversationStore``. They operate on plain message dicts
so any ``ConversationStore`` backend (local files, Mongo, etc.) can reuse the same algorithm
without duplicating it. The helpers they depend on (``_msg_role``, ``_is_user``,
``_render_turn``, ``_relevance_doc``) and the shared constants are defined at module level
alongside them.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tfdfidf_sampling import extract_terms, keywords_from_text, select_representatives

# ---------------------------------------------------------------------------
# Shared constants used by select_current_slice / select_related (and the
# SessionFileConversationStore which delegates to them).
# ---------------------------------------------------------------------------

# How preferred a USER turn is over an AI turn in relevance scoring. USER turns are PREFERRED:
# they carry the actual intent, so they get a multiplicative boost in select_representatives.
_USER_SCORE_BOOST: float = 1.5
# Verbatim USER turns are only compacted if they are absurdly long; AI turns are always compacted.
_USER_VERBATIM_CAP: int = 2000
_AI_COMPACT_CHARS: int = 400
# How strongly sharing terms with the CURRENT QUERY boosts a candidate's score, relative to its
# base TF-DF-IDF distinctiveness. High enough that any real overlap with the query outweighs the
# tiny recency boost (ts/1e11) and plain distinctiveness, so recall is relevance-first.
_QUERY_OVERLAP_WEIGHT: float = 4.0


def nl_terms(text: str) -> set:
    """WORD-LEVEL terms of natural-language text, for query-relevance matching.

    ``extract_terms`` splits only on punctuation-followed-by-space (built for file paths and
    digest strings), so prose collapses into whole-phrase terms that never overlap a query.
    Relevance comparisons between conversational text and the user's input must therefore use
    word-level tokens (``keywords_from_text``).
    """
    return set(keywords_from_text(text or ""))


def query_overlap_boost(terms: set, query_terms: set) -> float:
    """Multiplicative score boost for how much ``terms`` overlaps the current query's terms.

    ``1.0`` (no boost) when there is no query or no overlap; grows with the FRACTION of query
    terms covered, scaled by ``_QUERY_OVERLAP_WEIGHT``. This is what makes selection actually
    query-sensitive: TF-DF-IDF alone scores distinctiveness, not relevance to the input. Both
    sets should be WORD-LEVEL tokens (``nl_terms``) so prose actually overlaps.
    """
    if not query_terms or not terms:
        return 1.0
    overlap = len(terms & query_terms) / len(query_terms)
    return 1.0 + _QUERY_OVERLAP_WEIGHT * overlap


# ---------------------------------------------------------------------------
# Per-message helpers shared across selection functions.
# ---------------------------------------------------------------------------

def _msg_role(msg: Any) -> str:
    """Lower-case role of one message dict, or 'unknown'."""
    if isinstance(msg, dict):
        return str(msg.get("role", "unknown")).lower()
    return "unknown"


def _is_user(msg: Any) -> bool:
    return _msg_role(msg) == "user"


def _render_turn(msg: Any) -> str:
    """Render one message as a role-labelled line.

    USER turns render VERBATIM (capped only if absurdly long). Every other role (AI/assistant,
    tool, etc.) is COMPACTED so a long AI answer cannot dominate the rendered slice. This matches
    conversation_to_text's per-turn ``ROLE: text`` shape.
    """
    if isinstance(msg, dict):
        role = str(msg.get("role", "unknown")).upper()
        text = message_text(msg)
        if _is_user(msg):
            rendered = compact_message(text, max_chars=_USER_VERBATIM_CAP)
        else:
            rendered = compact_message(text, max_chars=_AI_COMPACT_CHARS)
        return f"{role}: {rendered}"
    return str(msg)


def _relevance_doc(msg: Any) -> str:
    """The TF-DF-IDF *document* for a turn: a USER turn's full text, but an AI turn's COMPACT form
    (so a long AI answer cannot dominate df/idf). Used for both term extraction and scoring."""
    text = message_text(msg)
    if _is_user(msg):
        return text
    return compact_message(text, max_chars=_AI_COMPACT_CHARS)

# Marker inserted where a long message is elided during compaction. Plain "..." (no em dash).
_ELLIPSIS = " [...] "

# Split a block of text into sentence/line chunks for sentence-granularity TF-DF-IDF selection.
# Newlines are hard boundaries; within a line we split on sentence-ending punctuation followed by
# whitespace. Each resulting non-empty chunk becomes one "document" for select_representatives.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_chunks(text: str) -> List[str]:
    """Split ``text`` into sentence/line chunks (newlines + sentence boundaries), trimmed, no empties."""
    chunks: List[str] = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        for piece in _SENTENCE_SPLIT_RE.split(line):
            piece = piece.strip()
            if piece:
                chunks.append(piece)
    return chunks


def compact_message(text: str, *, max_chars: int = 400) -> str:
    """Compact a long message to its head + tail plus the few most salient MIDDLE chunks.

    Short messages (within ``max_chars``) are returned unchanged. A long message is reduced to:
    its FIRST chunk (sentence/line), its LAST chunk, and the most salient MIDDLE chunks selected
    by TF-DF-IDF at sentence/newline granularity (each chunk is one document, scored with the
    shared ``extract_terms`` / ``select_representatives``). The pieces are joined chronologically
    with an ellipsis marker and the whole result is capped at ``max_chars``. Never raises -- any
    failure falls back to a simple head+tail truncation. The point is that a long (usually AI)
    message cannot dominate downstream df/idf: the compact form is what gets scored.
    """
    try:
        text = text or ""
        if len(text) <= max_chars:
            return text

        chunks = _split_chunks(text)
        if len(chunks) <= 1:
            # No sentence structure to exploit: fall back to head + tail of the raw text.
            return _head_tail(text, max_chars)
        if len(chunks) == 2:
            return _join_within_budget([chunks[0], chunks[1]], max_chars)

        head, tail = chunks[0], chunks[-1]
        middle = chunks[1:-1]

        # How many middle chunks can we afford after head + tail + ellipsis markers?
        reserved = len(head) + len(tail) + 2 * len(_ELLIPSIS)
        budget_for_middle = max(0, max_chars - reserved)

        selected_middle: List[str] = []
        if middle and budget_for_middle > 0:
            # Index the middle chunks so we can restore chronological order after selection.
            idx = [str(i) for i in range(len(middle))]
            picked = select_representatives(
                idx,
                get_terms=lambda i: extract_terms(middle[int(i)]),
                samples_per_group=max(1, min(len(middle), 3)),
            )
            picked_sorted = sorted(int(i) for i in picked)
            for i in picked_sorted:
                chunk = middle[i]
                if sum(len(c) for c in selected_middle) + len(chunk) > budget_for_middle:
                    continue
                selected_middle.append(chunk)

        parts = [head] + selected_middle + [tail]
        return _join_within_budget(parts, max_chars)
    except Exception:  # noqa: BLE001 — compaction must never raise
        return _head_tail(text or "", max_chars)


def _join_within_budget(parts: List[str], max_chars: int) -> str:
    """Join ``parts`` with the ellipsis marker, then hard-cap the result at ``max_chars``."""
    joined = _ELLIPSIS.join(p for p in parts if p)
    if len(joined) > max_chars:
        joined = _head_tail(joined, max_chars)
    return joined


def _head_tail(text: str, max_chars: int) -> str:
    """Simple fallback: keep the beginning and the end of ``text`` within ``max_chars``."""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_ELLIPSIS):
        return text[:max_chars]
    keep = max_chars - len(_ELLIPSIS)
    head_len = keep - keep // 2
    tail_len = keep - head_len
    return text[:head_len] + _ELLIPSIS + (text[-tail_len:] if tail_len else "")


def is_claude_conversation(data: Any) -> bool:
    """True if a JSON object looks like a Claude conversation.

    A valid conversation has a ``messages`` or ``turns`` array whose entries each carry a ``role``
    and some text (``text`` or ``content``).
    """
    if not isinstance(data, dict):
        return False

    messages = data.get("messages") or data.get("turns")
    if not isinstance(messages, list) or not messages:
        return False

    for msg in messages:
        if not isinstance(msg, dict):
            return False
        has_role = "role" in msg
        has_text = "text" in msg or "content" in msg
        if not (has_role and has_text):
            return False

    return True


def conversation_messages(conv: Any) -> List[Dict[str, Any]]:
    """Return the message list of a conversation (``messages`` or ``turns``), or []."""
    if not isinstance(conv, dict):
        return []
    msgs = conv.get("messages") or conv.get("turns") or []
    return msgs if isinstance(msgs, list) else []


def message_text(msg: Any) -> str:
    """Return the text of one message dict (``text`` or ``content``), or ''."""
    if isinstance(msg, dict):
        return str(msg.get("text") or msg.get("content") or "")
    return str(msg) if msg is not None else ""


def conversation_metadata(conv: Any) -> Dict[str, Any]:
    """Extract lightweight metadata from a conversation dict (rep, turns, model, message_count)."""
    metadata: Dict[str, Any] = {}
    if isinstance(conv, dict):
        if "rep_name" in conv:
            metadata["rep"] = conv["rep_name"]
        if "turn_count" in conv:
            metadata["turns"] = conv["turn_count"]
        if "model_hint" in conv:
            metadata["model"] = conv["model_hint"]
        metadata["message_count"] = len(conversation_messages(conv))
    return metadata


def conversation_to_text(conv: Any) -> str:
    """Render a conversation dict to readable text (a metadata header + role-labelled turns)."""
    parts: List[str] = []
    if isinstance(conv, dict):
        metadata = conversation_metadata(conv)
        if metadata:
            meta_str = " | ".join(f"{k}={v}" for k, v in metadata.items())
            parts.append(f"[{meta_str}]")
            parts.append("")
        for msg in conversation_messages(conv):
            if isinstance(msg, dict):
                role = msg.get("role", "unknown")
                parts.append(f"{role.upper()}: {message_text(msg)}")
            else:
                parts.append(str(msg))
    return "\n".join(parts)


def conversation_digest(conv: Any) -> str:
    """A lightweight digest (first message + last 2-3 messages + metadata) for cheap ranking."""
    parts: List[str] = []
    messages = conversation_messages(conv)

    metadata = conversation_metadata(conv)
    if metadata:
        meta_str = " | ".join(f"{k}={v}" for k, v in metadata.items())
        parts.append(f"[{meta_str}]")

    if messages:
        text = message_text(messages[0])
        if text:
            parts.append(f"START: {text[:200]}")

    if len(messages) > 1:
        sample_count = min(3, max(1, len(messages) // 2))
        for msg in messages[-sample_count:]:
            text = message_text(msg)
            if text:
                parts.append(f"RECENT: {text[:150]}")

    return " ".join(parts) if parts else "empty conversation"


def conversation_timestamp(conv: Any) -> float:
    """Unix timestamp for recency sorting, or 0.0 if none is recorded."""
    if isinstance(conv, dict):
        for field in ("updated_at", "updatedAt", "createdAt", "created_at", "timestamp"):
            if field in conv:
                val = conv[field]
                if isinstance(val, (int, float)):
                    return float(val)
    return 0.0


def scan_conversation_files(
    corpus_root: Optional[Path], sessions_dir: Path
) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Index conversation JSON FILE PATHS without parsing any file (the cheap stage-0 scan).

    Scans (recursively, when ``corpus_root`` is given) for ``.claude``/``conversations`` dirs plus
    the explicit ``sessions_dir`` and returns:

      * ``filepaths``      -- {unique_key: resolved Path} for every ``*.json`` found
      * ``conv_id_lookup`` -- {filename_stem: unique_key} (last-scanned wins on collision)

    No file content is read, so this stays cheap no matter how many conversations exist. Whether a
    file actually IS a conversation is decided lazily by whoever loads it
    (``is_claude_conversation`` at parse time). Never raises — unreadable dirs are silently skipped.
    """
    filepaths: Dict[str, Path] = {}
    conv_id_lookup: Dict[str, str] = {}

    search_dirs: List[Path] = []
    if corpus_root and corpus_root.is_dir():
        try:
            search_dirs.extend(corpus_root.glob("**/.claude"))
            search_dirs.extend(corpus_root.glob("**/conversations"))
        except Exception:  # noqa: BLE001
            pass
    if sessions_dir.is_dir():
        search_dirs.append(sessions_dir)

    if not search_dirs:
        return filepaths, conv_id_lookup

    seen = set()
    unique_dirs: List[Path] = []
    for d in search_dirs:
        d_abs = d.resolve()
        if d_abs not in seen:
            seen.add(d_abs)
            unique_dirs.append(d)

    for search_dir in unique_dirs:
        try:
            for session_file in search_dir.glob("*.json"):
                try:
                    file_stem = session_file.stem
                    if corpus_root:
                        try:
                            rel_path = session_file.relative_to(corpus_root)
                            unique_key = str(rel_path.with_suffix("")).replace("/", ":")
                        except ValueError:
                            unique_key = file_stem
                    else:
                        unique_key = file_stem
                    filepaths[unique_key] = session_file.resolve()
                    conv_id_lookup[file_stem] = unique_key
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    return filepaths, conv_id_lookup


def load_conversations(
    corpus_root: Optional[Path], sessions_dir: Path
) -> Tuple[Dict[str, Any], Dict[str, Path], Dict[str, str]]:
    """Load all Claude conversation JSON files, best-effort (the eager FULL load).

    Scans via ``scan_conversation_files`` then parses every found file. Returns:

      * ``conversations``      -- {unique_key: conversation dict}
      * ``filepaths``          -- {unique_key: resolved Path}
      * ``conv_id_lookup``     -- {filename_stem: unique_key} (last-loaded wins on collision)

    Cost grows with the total size of ALL session files, so a caller facing a large or ever-growing
    conversation dir should prefer the lazy two-stage path (``scan_conversation_files`` + per-file
    cached digests, as ``SessionFileConversationStore`` does) over this. Never raises — unreadable
    or non-conversation files are silently skipped.
    """
    conversations: Dict[str, Any] = {}
    filepaths: Dict[str, Path] = {}
    conv_id_lookup: Dict[str, str] = {}

    all_paths, _ = scan_conversation_files(corpus_root, sessions_dir)
    for unique_key, session_file in all_paths.items():
        try:
            with open(session_file) as f:
                data = json.load(f)
            if not is_claude_conversation(data):
                continue
            conversations[unique_key] = data
            filepaths[unique_key] = session_file
            conv_id_lookup[session_file.stem] = unique_key
        except (json.JSONDecodeError, OSError):
            pass

    return conversations, filepaths, conv_id_lookup


def truncate_transcript_middle(text: str, max_chars: int) -> str:
    """Bound a rendered TRANSCRIPT to ``max_chars`` by eliding the MIDDLE, keeping the opening
    context and (mostly) the recent tail.

    Head-only truncation is wrong for conversations: the turns that most often matter to a new
    request are the most recent ones, at the END of the transcript. This keeps roughly the first
    third and the last two thirds of the budget with a clear elision marker between them. Short
    text is returned unchanged. Never raises.
    """
    try:
        text = text or ""
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        marker = "\n[... middle of conversation elided ...]\n"
        if max_chars <= len(marker):
            return text[-max_chars:]
        keep = max_chars - len(marker)
        head_len = keep // 3
        tail_len = keep - head_len
        return text[:head_len] + marker + text[-tail_len:]
    except Exception:  # noqa: BLE001 — truncation must never raise
        return (text or "")[:max_chars]


def resolve_conv_key(
    conv_id: str, conversations: Dict[str, Any], conv_id_lookup: Dict[str, str]
) -> Optional[str]:
    """Resolve a conv id / filename stem / path tail to a unique key, or None if not found."""
    key = (conv_id or "").split("/")[-1]
    if key in conversations:
        return key
    if key in conv_id_lookup:
        return conv_id_lookup[key]
    # Also accept the raw value (it may already be a unique key with ':' separators).
    if conv_id in conversations:
        return conv_id
    return None


# ---------------------------------------------------------------------------
# Pure selection / rendering algorithms
# ---------------------------------------------------------------------------

def select_current_slice(
    messages: List[Dict[str, Any]],
    query: str,
    *,
    recent_turns: int = 4,
    max_chars: int = 6000,
) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Select and render a relevant slice of a single conversation's messages.

    Pure function: operates on a plain list of message dicts and returns a
    3-tuple ``(rendered_text, turn_metadata_list, truncated_flag)`` that any
    ``ConversationStore`` backend can wrap in its own ``ConversationContext``.

    Algorithm (identical to the one previously embedded in
    ``SessionFileConversationStore.current_slice``):

    * The ONE guaranteed turn is the last USER turn (fall back to the last message of any role).
      This is the ANCHOR and is NEVER dropped by ``max_chars`` truncation.
    * ``recent_turns`` is the "considered window": the last N turns join the candidate pool (not
      auto-in). Older turns are candidates too. The anchor is selected separately.
    * Per-turn relevance is LENGTH-NORMALIZED (``1/sqrt(len(terms))``) so length alone never wins.
      USER turns get a ``x1.5`` score boost (``_USER_SCORE_BOOST``). Recent turns get a mild
      ``x1.05`` nudge as a tiebreak (not auto-inclusion).
    * AI turns are COMPACTED via ``compact_message`` for both scoring and rendering; USER turns
      render VERBATIM (capped only if absurdly long).
    * Returns ``(text, turns_meta, truncated)`` where ``turns_meta`` is
      ``[{"role": _msg_role(m)} for m in included]``.

    Never raises (callers guard with their own try/except if desired).
    """
    if not messages:
        return ("", [], False)

    # The ONE guaranteed turn: the last USER turn (fall back to last message of any role).
    anchor_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if _is_user(messages[i])),
        len(messages) - 1,
    )

    # The "considered window": the last N turns join the candidate pool (not auto-in). Older
    # turns are candidates too. The anchor is selected separately and always kept.
    n = max(0, int(recent_turns))
    window_start = max(0, len(messages) - n) if n else 0
    candidate_idxs = [i for i in range(len(messages)) if i != anchor_idx]

    def _terms(idx_str: str) -> set:
        i = int(idx_str)
        return extract_terms(_relevance_doc(messages[i]))

    query_word_terms = nl_terms(query or "")

    def _boost(idx_str: str) -> float:
        i = int(idx_str)
        msg = messages[i]
        # Length-normalize so a long turn cannot win on size alone: divide the raw TF-DF-IDF
        # score by the (sub-linear) document length. USER turns are PREFERRED via x1.5. Turns
        # sharing WORDS with the QUERY are boosted so selection is relevance-first, not just
        # distinctiveness-first (word-level tokens: prose phrase-terms never overlap a query).
        doc = _relevance_doc(msg)
        length_norm = 1.0 / math.sqrt(max(1, len(extract_terms(doc))))
        user_boost = _USER_SCORE_BOOST if _is_user(msg) else 1.0
        window_nudge = 1.05 if i >= window_start else 1.0  # mild recency tiebreak, not auto-in
        relevance = query_overlap_boost(nl_terms(doc), query_word_terms)
        return length_norm * user_boost * window_nudge * relevance

    selected_idxs: List[int] = []
    if candidate_idxs:
        # How many candidates to keep: a few, scaled by the considered window.
        samples = max(1, min(len(candidate_idxs), max(2, n)))
        picked = select_representatives(
            [str(i) for i in candidate_idxs],
            get_terms=_terms,
            samples_per_group=samples,
            get_score_boost=_boost,
        )
        selected_idxs = sorted(int(i) for i in picked)

    included_idxs = sorted(set(selected_idxs) | {anchor_idx})
    included = [messages[i] for i in included_idxs]

    # Render: anchor line is protected from truncation; others may be dropped oldest-first.
    anchor_pos = included_idxs.index(anchor_idx)
    rendered_lines = [_render_turn(m) for m in included]
    truncated = False
    text = "\n".join(rendered_lines)
    # Drop oldest non-anchor lines first while over budget.
    while len(text) > max_chars and len(rendered_lines) > 1:
        drop_at = 0 if anchor_pos != 0 else 1  # never drop the anchor
        if drop_at >= len(rendered_lines) or drop_at == anchor_pos:
            break
        rendered_lines.pop(drop_at)
        if drop_at < anchor_pos:
            anchor_pos -= 1
        truncated = True
        text = "\n".join(rendered_lines)
    # If the anchor line ALONE still exceeds budget, keep it but cap it (never drop it).
    if len(text) > max_chars:
        anchor_line = rendered_lines[anchor_pos]
        if len(anchor_line) >= len(text) and len(anchor_line) > max_chars:
            rendered_lines[anchor_pos] = anchor_line[:max_chars]
            text = "\n".join(rendered_lines)
        if len(text) > max_chars:
            # Last resort: keep the anchor line only.
            text = rendered_lines[anchor_pos][:max_chars]
        truncated = True

    turns_meta = [{"role": _msg_role(m)} for m in included]
    return (text, turns_meta, truncated)


def rank_candidates_by_digest(
    keys: List[str],
    *,
    digest_of: Callable[[str], str],
    timestamp_of: Callable[[str], float],
    query: str,
    top_n: int,
) -> List[str]:
    """Rank conversation keys by RELEVANCE of their DIGEST text to ``query``, full horizon.

    Scoring: TF-DF-IDF distinctiveness x a strong query-overlap boost (``query_overlap_boost``,
    what makes ranking actually query-sensitive) x length normalization (a long digest cannot win
    on size alone) x a small recency boost (``1.0 + ts / 1e11``, a tie-break, never a cutoff --
    an old digest still wins purely on relevance).

    Pure and cheap: works off digest text + timestamp alone, no message content needed, so it is
    safe to call over an ENTIRE (possibly large and ever-growing) candidate pool as a first-stage
    prefilter before any full conversation is loaded. Shared by ``select_related`` (the final,
    precise pick) and ``SessionFileConversationStore.related_slices`` (the cheap shortlist stage
    that bounds how many files get fully loaded). Never raises.
    """
    if not keys:
        return []
    # Word-level tokens throughout: digests and queries are prose, and phrase-level
    # ``extract_terms`` tokens never overlap between two prose texts (see ``nl_terms``).
    query_terms = nl_terms(query or "")
    terms_by_key = {k: nl_terms(digest_of(k)) for k in keys}

    def _boost(k: str) -> float:
        ts = timestamp_of(k)
        recency = 1.0 + (ts / 1e11) if ts > 0 else 1.0
        length_norm = 1.0 / math.sqrt(max(1, len(terms_by_key[k])))
        relevance = query_overlap_boost(terms_by_key[k], query_terms)
        return recency * length_norm * relevance

    n = max(1, int(top_n))
    ranked = select_representatives(keys, get_terms=lambda k: terms_by_key[k],
                                    samples_per_group=n, get_score_boost=_boost)
    return ranked[:n]


def select_related(
    conversations: List[Dict[str, Any]],
    query: str,
    *,
    max_convs: int = 3,
    max_chars: int = 6000,
    get_conv_id: Optional[Callable[[int, Dict[str, Any]], str]] = None,
    must_include_ids: Optional[set] = None,
) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Select and render slices from a list of related conversations.

    Pure function: accepts a pre-filtered list of conversation dicts (scope filtering is the
    caller's responsibility) and returns ``(rendered_text, sources_list, truncated_flag)`` where
    each source entry is ``{"conv_id": <id>, "label": "related conversation"}``.

    Algorithm:

    * Each conversation is ranked via ``rank_candidates_by_digest`` (query-overlap-boosted
      TF-DF-IDF over its ``conversation_digest`` with a small recency boost).
    * At most ``max_convs`` conversations are rendered from the ranking. Conversations whose
      resolved id is in ``must_include_ids`` (e.g. a caller's recency floor) are ADDITIONALLY
      rendered even when not ranked in, appended after the ranked winners.
    * Per conversation: a short slice (last 4 turns, rendered with AI compaction) or the digest
      is used, formatted as ``=== Related conversation: {id} ===\\n{body}``.
    * ``max_chars`` is respected: truncation stops adding blocks once the budget is hit, with a
      final hard cap on the joined text.

    ``get_conv_id``: an optional callable ``(index, conv_dict) -> str`` for resolving the id of
    each conversation. Defaults to ``conv.get("id") or conv.get("conv_id") or str(index)``.

    Never raises (callers guard with their own try/except if desired).
    """
    if not conversations:
        return ("", [], False)

    def _resolve_id(i: int, conv: Dict[str, Any]) -> str:
        if get_conv_id is not None:
            return get_conv_id(i, conv)
        return str(conv.get("id") or conv.get("conv_id") or i)

    indices = [str(i) for i in range(len(conversations))]
    chosen = rank_candidates_by_digest(
        indices,
        digest_of=lambda idx_str: conversation_digest(conversations[int(idx_str)]),
        timestamp_of=lambda idx_str: conversation_timestamp(conversations[int(idx_str)]),
        query=query,
        top_n=max_convs,
    )
    if must_include_ids:
        for idx_str in indices:
            if idx_str in chosen:
                continue
            if _resolve_id(int(idx_str), conversations[int(idx_str)]) in must_include_ids:
                chosen.append(idx_str)

    parts: List[str] = []
    sources: List[Dict[str, Any]] = []
    truncated = False
    used = 0
    for idx_str in chosen:
        i = int(idx_str)
        conv = conversations[i]
        conv_id = _resolve_id(i, conv)
        # Short slice: last few turns, so it stays compact.
        msgs = conversation_messages(conv)
        tail = msgs[-4:] if msgs else []
        body = "\n".join(_render_turn(m) for m in tail) or conversation_digest(conv)
        block = f"=== Related conversation: {conv_id} ===\n{body}"
        if used + len(block) > max_chars and parts:
            truncated = True
            break
        parts.append(block)
        sources.append({"conv_id": conv_id, "label": "related conversation"})
        used += len(block)

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return (text, sources, truncated)
