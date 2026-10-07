"""Conversation-turn cards for the ContextAssembler system."""
import datetime
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.tfdfidf_sampling import keywords_from_text, select_representatives


def scope_tags_for(meta: Optional[Dict[str, Any]]) -> List[str]:
    """The scope tags a turn carries: its explicit ``scope_tags``, else its quest ids as tags."""
    from .recent_context import quest_scope_key
    from .scope_tags import as_tag_list, union_scope_tags
    m = meta or {}
    explicit = as_tag_list(m.get("scope_tags"))
    if explicit:
        return explicit
    ids = [m.get("quest_id")] + list(m.get("quest_ids") or [])
    return union_scope_tags([quest_scope_key(q) for q in ids if q])


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# How much of a past turn's USER side is shown, and how much of it is kept on the card.
#
# A turn's "user" text is whatever the run was asked, and for a queued task that is the whole
# composed brief: standing instructions, the goal frame, a context-updates block with its receipt
# gate, the last run's output. Shown verbatim, five of them made up two thirds of an 838K-character
# deep prompt (2026-10-06), each nesting the briefs before it. A past turn is a pointer to what was
# discussed, not a second copy of it, so it is shown as its opening and kept on the card at a
# bounded length (the vector arm embeds ``description``, which is bounded the same way).
MAX_USER_CHARS = 600
MAX_STORED_USER_CHARS = 4000


def turn_excerpt(text: str, limit: int) -> str:
    """A past turn's text, cut for display: context-updates blocks removed, whitespace folded."""
    from .prompt_budget import STALE_UPDATES_NOTE, strip_blocks
    body = " ".join(strip_blocks(text or "", note=STALE_UPDATES_NOTE).split())
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# TurnContextStore
# ---------------------------------------------------------------------------


class TurnContextStore:
    """ContextAssembler that stores conversation turns as cards and retrieves relevant ones.

    Mirrors the FileContextStore card format so the optional vector arm can embed turn cards
    alongside file cards using the same pipeline. Retrieval is IDF-weighted keyword overlap
    over the stored cards (same approach as FileContextStore); when a VectorContextAssembler
    is wired in a CompositeContextAssembler, semantic retrieval over turn descriptions is
    automatic.

    Each completed turn is stored as a card via record(). assemble() retrieves the turns
    most relevant to the current message. The immediately preceding turn is always included
    (floor of 1 recent); older turns are scored by keyword overlap and trimmed to max_older.

    Usage in a consumer::

        from quest_ai_runner.core.turn_context_store import TurnContextStore
        from quest_ai_runner.core.composite_assembler import CompositeContextAssembler

        turn_store = TurnContextStore()
        cfg = RunnerConfig(
            ...,
            context_assembler=CompositeContextAssembler([file_store, turn_store]),
        )
    """

    def __init__(
        self,
        turns_dir: str = ".quest-context/turns",
        max_turns: int = 200,
        max_older: int = 4,
        max_assistant_chars: int = 400,
        provider: Optional[Any] = None,
        model: Optional[str] = None,
        max_user_chars: int = MAX_USER_CHARS,
    ):
        self._dir = Path(turns_dir)
        self._max_turns = max_turns
        self._max_older = max_older
        self._max_assistant_chars = max_assistant_chars
        self._max_user_chars = max_user_chars
        self._provider = provider
        self._model = model

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load_cards(self) -> List[Dict[str, Any]]:
        """Load all turn cards, sorted oldest first."""
        if not self._dir.exists():
            return []
        cards = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                cards.append(json.loads(p.read_text()))
            except Exception:
                pass
        return cards

    def _recency_boost(self, created_at: str) -> float:
        """Multiplicative recency boost passed to select_representatives. Half-life = 7 days."""
        try:
            ts = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            days_old = (now - ts).total_seconds() / 86400.0
            return 1.0 + math.exp(-days_old * math.log(2) / 7.0)
        except Exception:
            return 1.0

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Return relevant past turns as context_view. Never raises."""
        from .adapters import AssembledContext  # local import to avoid circular

        try:
            cards = self._load_cards()
            # QUEST FENCE (``core.scope_tags``): a turn recorded while working one quest never
            # surfaces in a turn scoped to another. Untagged cards stay visible, per the fence's
            # own rule, so history recorded before turns carried tags is not lost.
            turn_tags = (meta or {}).get("scope_tags")
            if turn_tags:
                from .scope_tags import scope_tags_allow
                cards = [c for c in cards if scope_tags_allow(c.get("scope_tags"), turn_tags)]
            # NOT ITS OWN THREAD. A thread's earlier runs reach its brief already, through the
            # thread's own run history and last result; retrieving them here as "past
            # conversations" showed the run its own previous passes a second time, whole.
            own_task = str((meta or {}).get("task_id") or "")
            if own_task:
                cards = [c for c in cards if str(c.get("task_id") or "") != own_task]
            if not cards:
                return AssembledContext()

            query_terms = set(keywords_from_text(task_text))
            # Per card: keep user vs AI keywords separate so user messages get a score
            # boost — user inputs are a stronger signal of what was discussed than AI outputs.
            # Fall back to the combined "keywords" field for cards written before this split.
            card_user_kw: Dict[int, set] = {
                i: set(c.get("user_keywords", c.get("keywords", []))) for i, c in enumerate(cards)
            }
            card_kw: Dict[int, set] = {i: set(c.get("keywords", [])) for i, c in enumerate(cards)}

            # Pre-filter to cards with any query overlap, then delegate scoring and
            # selection entirely to select_representatives (TF-DF-IDF + recency boost).
            overlapping = [i for i, kw in card_kw.items() if kw & query_terms]

            def _boost(i: int) -> float:
                user_overlap = len(card_user_kw[i] & query_terms)
                # Each user keyword match adds a 0.4 boost (capped at 3 terms → 1.2 max).
                user_bonus = 0.4 * min(user_overlap, 3)
                return (1.0 + user_bonus) * self._recency_boost(cards[i].get("created_at", ""))

            selected_indices = set(select_representatives(
                items=overlapping,
                get_terms=lambda i: card_kw[i],
                samples_per_group=self._max_older,
                get_score_boost=_boost,
            ))

            # Always include the most recent card.
            selected_indices.add(len(cards) - 1)

            # LLM filter over the selected set (optional, falls back silently).
            if self._provider is not None and selected_indices:
                try:
                    from .card_filter import filter_cards_by_relevance
                    # Each candidate is named by its opening, not its whole text: a past turn's
                    # user side can be an entire composed brief.
                    candidate_dicts = [
                        {"id": str(i), "title": turn_excerpt(cards[i].get("user", ""), 200),
                         "files": [], "adapter": "turn"}
                        for i in selected_indices
                    ]
                    kept_ids = {m.id for m in filter_cards_by_relevance(
                        task_text, candidate_dicts,
                        model_provider=self._provider, model=self._model,
                    )}
                    # Always keep the most recent card even if the LLM filters it.
                    kept_ids.add(str(len(cards) - 1))
                    selected_indices = {i for i in selected_indices if str(i) in kept_ids}
                except Exception:
                    pass

            ordered = [cards[i] for i in sorted(selected_indices)]
            lines = ["--- RELEVANT PAST CONVERSATIONS ---"]
            for card in ordered:
                date = card.get("created_at", "")[:10]
                lines.append(f"[{date}] User: "
                             f"{turn_excerpt(card.get('user', ''), self._max_user_chars)}")
                lines.append(f"         AI: "
                             f"{turn_excerpt(card.get('assistant_summary', ''), self._max_assistant_chars or 400)}")
            return AssembledContext(context_view="\n".join(lines))
        except Exception:
            from .adapters import AssembledContext
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Store this turn as a card. Never raises."""
        try:
            response = (outcome.get("response") or "").strip()
            if not task_text and not response:
                return
            self._ensure_dir()

            # Prune oldest cards if over limit
            existing = sorted(self._dir.glob("*.json"))
            while len(existing) >= self._max_turns:
                try:
                    existing[0].unlink()
                except Exception:
                    pass
                existing = existing[1:]

            user_kw = keywords_from_text(task_text)
            asst_kw = keywords_from_text(response)
            # Deduplicate while preserving order; user keywords first so they score higher
            # in IDF-based selection (user inputs are primary signal of what was discussed).
            all_kw = list(dict.fromkeys(user_kw + asst_kw))

            asst_summary = response
            if self._max_assistant_chars and len(asst_summary) > self._max_assistant_chars:
                asst_summary = asst_summary[: self._max_assistant_chars].rstrip() + "…"

            card_id = (
                f"turn-{time.time_ns()}-"
                f"{hashlib.sha1(task_text.encode()).hexdigest()[:8]}"
            )
            stored_user = turn_excerpt(task_text, MAX_STORED_USER_CHARS)
            card: Dict[str, Any] = {
                "id": card_id,
                "created_at": _now_iso(),
                "user": stored_user,
                "assistant_summary": asst_summary,
                # For vector embedding: bounded like ``user``, so one giant brief cannot become
                # one giant card.
                "description": (f"User: {stored_user}\nAssistant: "
                                f"{turn_excerpt(response, MAX_STORED_USER_CHARS)}"),
                # The quests this turn was scoped to (``core.scope_tags``), so ``assemble`` can
                # keep it out of turns about a different quest.
                "scope_tags": scope_tags_for(outcome),
                # The task (thread) this turn ran for, so its later runs do not retrieve it.
                "task_id": str(outcome.get("task_id") or ""),
                "user_keywords": user_kw,   # stored separately so assemble() can boost user-term matches
                "ai_keywords": asst_kw,     # the last AI output's terms (also important, kept distinct)
                "keywords": all_kw,         # combined set for backward-compat reads
                "files_consulted": outcome.get("files") or [],
            }
            path = self._dir / f"{card_id}.json"
            try:
                path.write_text(json.dumps(card, indent=2))
            except Exception:
                pass
        except Exception:
            pass


def assembler_renders_turns(assembler: Any) -> bool:
    """Whether ``assembler`` (or anything it composes) is a ``TurnContextStore``.

    A run must see past turns from ONE store. A lane that wires the org-wide turn store into its
    context assembler and also renders a per-rep turn store into the rep preamble showed every
    past brief twice (2026-10-06: about 289K characters each). The poller asks this before it
    renders the rep's own turns, so only one history reaches the prompt. Never raises.
    """
    from .composite_assembler import find_assembler
    return find_assembler(assembler, lambda a: isinstance(a, TurnContextStore)) is not None
