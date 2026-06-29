"""Conversation-turn cards for the ContextAssembler system."""
import datetime
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.tfdfidf_sampling import keywords_from_text, select_representatives


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
            if not cards:
                return AssembledContext()

            query_terms = set(keywords_from_text(task_text))
            card_kw: Dict[int, set] = {i: set(c.get("keywords", [])) for i, c in enumerate(cards)}

            # Pre-filter to cards with any query overlap, then delegate scoring and
            # selection entirely to select_representatives (TF-DF-IDF + recency boost).
            overlapping = [i for i, kw in card_kw.items() if kw & query_terms]
            selected_indices = set(select_representatives(
                items=overlapping,
                get_terms=lambda i: card_kw[i],
                samples_per_group=self._max_older,
                get_score_boost=lambda i: self._recency_boost(cards[i].get("created_at", "")),
            ))

            # Always include the most recent card.
            selected_indices.add(len(cards) - 1)

            # LLM filter over the selected set (optional, falls back silently).
            if self._provider is not None and selected_indices:
                try:
                    from .card_filter import filter_cards_by_relevance
                    candidate_dicts = [
                        {"id": str(i), "title": cards[i].get("user", ""), "files": [], "adapter": "turn"}
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
                lines.append(f"[{date}] User: {card.get('user', '')}")
                lines.append(f"         AI: {card.get('assistant_summary', '')}")
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
