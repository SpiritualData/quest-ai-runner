"""Conversation-turn cards for the ContextAssembler system -- stdlib only."""
import datetime
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Stopword set (same set as the original turn_memory so keyword extraction
# is consistent across the two modules).
# ---------------------------------------------------------------------------

_STOP = frozenset("""
a an the is are was were be been being have has had do does did will would could should may
might shall can need to of in on at by for with about as into through during before after
above below from and or but not this that these those i you he she it we they what which
who how when where why all both each few more most other some such no nor so yet either
neither s t re ve ll d m
""".split())


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


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
    ):
        self._dir = Path(turns_dir)
        self._max_turns = max_turns
        self._max_older = max_older
        self._max_assistant_chars = max_assistant_chars
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
        """Multiplicative recency boost: recent cards score higher. Half-life = 7 days."""
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
            if not cards:
                return AssembledContext()

            from quest_ai_runner.adapters.tfdfidf_sampling import compute_idf

            # Use _keywords() for natural-language term extraction (extract_terms is for file paths).
            # compute_idf() from tfdfidf_sampling provides the smoothed IDF formula.
            query_terms = set(_keywords(task_text))
            card_term_sets = [set(c.get("keywords", [])) for c in cards]
            idf = compute_idf(card_term_sets)

            scored = [
                (
                    sum(idf.get(t, 1.0) for t in query_terms if t in card_terms)
                    * self._recency_boost(c.get("created_at", "")),
                    i,
                    c,
                )
                for i, (c, card_terms) in enumerate(zip(cards, card_term_sets))
            ]
            scored.sort(key=lambda x: (-x[0], -x[1]))  # highest score, most recent first

            # All candidates: most recent first, limited to 2x max_older headroom
            all_candidates = [
                card for score, idx, card in scored
                if score > 0
            ][: (self._max_older + 1) * 2]
            # Always include the last turn as a candidate even if IDF score is 0
            last = cards[-1]
            if last not in all_candidates:
                all_candidates = [last] + all_candidates

            # LLM filter across all candidates (including the most recent turn)
            if self._provider is not None and all_candidates:
                try:
                    from .card_filter import filter_cards_by_relevance
                    candidate_dicts = [
                        {
                            "id": c.get("id", f"turn-{i}"),
                            "title": c.get("user", ""),
                            "files": [],
                            "adapter": "turn",
                        }
                        for i, c in enumerate(all_candidates)
                    ]
                    kept = filter_cards_by_relevance(
                        task_text, candidate_dicts,
                        model_provider=self._provider, model=self._model,
                    )
                    kept_ids = {m.id for m in kept}
                    all_candidates = [
                        c for c in all_candidates
                        if c.get("id", "") in kept_ids
                    ]
                except Exception:
                    pass  # silently fall back to IDF selection

            selected: Dict[int, Dict[str, Any]] = {}
            for card in all_candidates:
                if len(selected) >= self._max_older + 1:
                    break
                selected[id(card)] = card

            # Restore chronological order
            ordered = [c for c in cards if id(c) in selected]

            lines = ["--- RELEVANT PAST CONVERSATIONS ---"]
            for card in ordered:
                user = card.get("user", "")
                asst = card.get("assistant_summary", "")
                date = card.get("created_at", "")[:10]  # YYYY-MM-DD
                lines.append(f"[{date}] User: {user}")
                lines.append(f"         AI: {asst}")
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

            user_kw = _keywords(task_text)
            asst_kw = _keywords(response)
            all_kw = list(dict.fromkeys(user_kw + asst_kw))

            asst_summary = response
            if self._max_assistant_chars and len(asst_summary) > self._max_assistant_chars:
                asst_summary = asst_summary[: self._max_assistant_chars].rstrip() + "…"

            card_id = (
                f"turn-{time.time_ns()}-"
                f"{hashlib.sha1(task_text.encode()).hexdigest()[:8]}"
            )
            card: Dict[str, Any] = {
                "id": card_id,
                "created_at": _now_iso(),
                "user": task_text,
                "assistant_summary": asst_summary,
                "description": f"User: {task_text}\nAssistant: {response}",  # full, for vector embedding
                "keywords": all_kw,
                "files_consulted": outcome.get("files") or [],
            }
            path = self._dir / f"{card_id}.json"
            try:
                path.write_text(json.dumps(card, indent=2))
            except Exception:
                pass
        except Exception:
            pass
