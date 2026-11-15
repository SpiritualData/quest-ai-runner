"""The interface of IntentEvolving: a Protocol declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping

from esoi import solution


@solution("intent-evolution", kind="event", entry="suggest",
          example={"quest_id": "quest-id", "intents": ["Ship the launch note"], "metrics": [], "experiments": [], "history": []})
@typing.runtime_checkable
class IntentEvolving(typing.Protocol):
    """Helps a person's intents move: for a quest with its intents, metrics, history and experiments it returns the option most likely to work
    with the metric that checks it, or an experiment when the best option is unknown, or nothing when restraint is right:
    ``{"kind": "option" | "experiment" | "none", "text", "metric"}``. It changes nothing; an accepted proposal is made through Quest Change Executor."""

    def suggest(self, quest: Mapping[str, Any]) -> Mapping[str, Any]: ...
