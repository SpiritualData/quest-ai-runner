"""The two instruction levels an autopilot's brief is built from: the quest-wide brief and the autopilot's own, each with a built-in default.

Who works and when is not decided here any more (``autopilot_units``): the backend serves each autopilot resolved, with its rep and days.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import (
    BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS, BUNDLED_DEFAULT_QUEST_INSTRUCTIONS, MAX_INSTRUCTIONS_CHARS, log)


def capped_instructions(text: Any, label: str) -> str:
    """Instructions as the person wrote them, truncated at the cap the backend enforces on write (values older than the cap are the only way this fires).

    The text itself is never logged: it is the person's private content and can run to 12 KB.
    """
    value = str(text or "").strip()
    if len(value) > MAX_INSTRUCTIONS_CHARS:
        log.warning("autopilot: %s instructions truncated from %d to %d characters", label, len(value), MAX_INSTRUCTIONS_CHARS)
        value = value[:MAX_INSTRUCTIONS_CHARS]
    return value


def default_quest_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The QUEST-WIDE brief a batch works to when the quest itself carries none: how to work here
    and how to deliver, for every character on the quest.

    The SERVER'S value wins whenever the payload carries one:
    ``autopilot.default_quest_instructions`` is derived read-only by quest-backend, so the text a
    run works to is whatever that backend currently says it is, and improving it there improves
    every quest that never wrote its own. ``BUNDLED_DEFAULT_QUEST_INSTRUCTIONS`` is only what an
    older backend (or any client that does not serve the field) degrades to, so a character on duty
    still has a specification instead of a silent pass. See that constant for why the copy exists
    at all.
    """
    served = str(autopilot_cfg.get("default_quest_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_QUEST_INSTRUCTIONS


def default_persona_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The PERSONA brief a batch works to when that character carries none: what to work on.

    Resolved exactly like ``default_quest_instructions_for`` and from its own served field
    (``autopilot.default_persona_instructions``), because the two slots default independently: a
    quest that wrote a quest-wide brief and no character brief still needs this one, and the
    reverse is just as common.
    """
    served = str(autopilot_cfg.get("default_persona_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS
