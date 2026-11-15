"""Reading what a run's brief needs from the quest that no other part owns (the intent frame).

A mixin of ``AutopilotPass`` (see ``autopilot.py``): methods only, state lives on the instance. A run is ONE ``autopilot_run`` task created by the backend,
so there is nothing to create here: the brief is composed by ``AutopilotPass._compose_run`` and the executor runs it.
"""
from __future__ import annotations

from typing import Optional

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import log


class WorkMixin:
    """Reading what a run's brief needs from the quest."""

    def read_intent_frame(self, quest_id: str) -> Optional[str]:
        """The quest's intent frame text, or None (no intents, unsorted, a client that cannot ask, a failed
        read). The server builds it from stored ids with no model call."""
        getter = getattr(self._client, "get_intent_frame", None)
        if not callable(getter):
            return None
        try:
            frame = getter(quest_id)
        except Exception as e:  # noqa: BLE001 -- an extra for the brief, never a reason to lose the pass
            log.warning("autopilot: intent frame unavailable for quest %s: %s", quest_id, e)
            return None
        text = str((frame or {}).get("text") or "").strip() if isinstance(frame, dict) else ""
        return text or None
