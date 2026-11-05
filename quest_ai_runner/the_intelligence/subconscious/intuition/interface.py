"""The interface of PredictionStore: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from esoi import solution
from quest_ai_runner.the_intelligence.subconscious.intuition.anticipation_learner import Pattern, Prediction


@solution("the-intuition", kind="event", entry="load_predictions", example="quest-id")
@runtime_checkable
class PredictionStore(Protocol):
    """The port the anticipation engine persists through: patterns learned per scope, the scope's live
    predictions with their precomputed context views, and an append-only outcome log. A consumer backs it
    with its own database; every method is best-effort and must never raise.
    """

    def load_patterns(self, scope_key: str) -> List[Pattern]: ...

    def save_patterns(self, scope_key: str, patterns: List[Pattern]) -> None: ...

    def load_predictions(self, scope_key: str) -> List[Prediction]: ...

    def save_predictions(self, scope_key: str, predictions: List[Prediction],
                         views: Optional[Dict[str, str]] = None) -> None: ...

    def load_view(self, scope_key: str, prediction_id: str) -> str: ...

    def clear_predictions(self, scope_key: str) -> None: ...

    def append_log(self, record: Dict[str, Any]) -> None: ...
