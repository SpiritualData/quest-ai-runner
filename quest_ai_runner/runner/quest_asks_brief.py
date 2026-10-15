"""quest_asks_brief -- the "What You Asked" section of an autopilot batch.

The quest's asks ledger (``GET /api/asks``) holds what people asked of the AI on this quest, and
the default autopilot instructions tell a run to work the open ones first. This module turns that
ledger, plus the asks already waiting on a person (decision-requests), into the compact, budgeted
text the batch carries, following ``quest_experiments`` as the precedent: read once per quest,
``None`` for an unreadable list so a failed read renders as no section rather than "nothing was
asked", and the section carries its own short instruction so it rides in whole or not at all.

Which rows count is the ledger's own vocabulary (``feedback_ledger.OPEN_STATES``), minus
``awaiting acceptance``: a run that has already claimed an ask done is not owed more work on it.
A standing rule that is in force is not open but is shown, as a rule to follow. A row whose kind
is still ``unknown`` has never been sorted, and is marked so the run sorts it with ``update_ask``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .autopilot_periods import _parse_dt
from .feedback_ledger import (AWAITING_ACCEPTANCE, IN_FORCE, KIND_STANDING, KIND_UNKNOWN,
                              OPEN_STATES)

log = logging.getLogger("quest-ai-runner.quest_asks_brief")

#: Characters the section may use (about 900 tokens).
ASKS_BRIEF_MAX_CHARS = 3600
#: How many rows of each list are shown before "+N more".
MAX_OPEN_ASKS = 12
MAX_RULES = 6
MAX_WAITING = 8
#: Characters of a person's words shown per ask.
ASK_TEXT_LIMIT = 260

HEADER = ("WHAT YOU ASKED. What people asked of you on this quest, and what is already waiting on "
          "them. Work the open asks first; the key in front of each is what update_ask needs.")


def fetch_asks(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """The quest's ask rows, or ``None`` when they could not be read. Never raises."""
    lister = getattr(client, "list_asks", None)
    if not callable(lister):
        return None
    try:
        rows = lister(quest_id=quest_id, limit=200)
        return None if rows is None else [r for r in rows if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 -- additive context; never block a pass
        log.info("could not read asks for quest %s (%s: %s)", quest_id, type(e).__name__, e)
        return None


def fetch_open_decisions(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """The asks already waiting on a person, or ``None`` when they could not be read."""
    lister = getattr(client, "list_open_decisions_for_quest", None)
    if not callable(lister):
        return None
    try:
        return [r for r in (lister(quest_id) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001
        log.info("could not read open decisions for quest %s (%s: %s)", quest_id, type(e).__name__, e)
        return None


def one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def age_label(row: Dict[str, Any], now: datetime) -> str:
    when = _parse_dt(row.get("occurred_at") or row.get("created_at"))
    if when is None:
        return ""
    days = max(0, (now - when).days)
    return "today" if days == 0 else f"{days}d ago"


def is_owed(row: Dict[str, Any]) -> bool:
    state = str(row.get("state") or "open")
    return state in OPEN_STATES and state != AWAITING_ACCEPTANCE


def ask_entry(row: Dict[str, Any], now: datetime) -> str:
    kind = str(row.get("kind") or KIND_UNKNOWN)
    label = f"{row.get('state') or 'open'}, " + ("NOT SORTED" if kind == KIND_UNKNOWN else kind)
    who = ", ".join(p for p in (str(row.get("author") or ""), age_label(row, now)) if p)
    return (f"- {row.get('key')} [{label}]" + (f" {who}:" if who else "") + " "
            + one_line(row.get("text"), ASK_TEXT_LIMIT))


def rule_entry(row: Dict[str, Any]) -> str:
    return f"- {row.get('key')} {one_line(row.get('text'), ASK_TEXT_LIMIT)}"


def waiting_entry(row: Dict[str, Any]) -> str:
    return f"- [{row.get('kind') or 'approve'}] {one_line(row.get('summary'), 140)}"


def capped(lines: Iterable[str], limit: int) -> List[str]:
    rows = list(lines)
    return rows[:limit] + ([f"- +{len(rows) - limit} more"] if len(rows) > limit else [])


def asks_brief_section(asks: Optional[List[Dict[str, Any]]],
                       decisions: Optional[List[Dict[str, Any]]] = None, *,
                       now: Optional[datetime] = None,
                       max_chars: int = ASKS_BRIEF_MAX_CHARS) -> str:
    """The section text, or ``""`` when the asks could not be read or there is nothing to say.

    ``decisions`` (``None`` = unreadable) adds what is already waiting on a person, so a run does
    not raise it twice; it never makes a section on its own, since the asks are the point.
    """
    if asks is None:
        return ""
    when = now or datetime.now(timezone.utc)
    owed = sorted((r for r in asks if is_owed(r)),
                  key=lambda r: str(r.get("occurred_at") or r.get("first_seen_at") or ""))
    rules = [r for r in asks if r.get("kind") == KIND_STANDING and r.get("state") == IN_FORCE]
    if not owed and not rules:
        return ""
    lines = [HEADER]
    if owed:
        lines += ["", "Open asks:"] + capped((ask_entry(r, when) for r in owed), MAX_OPEN_ASKS)
        if any(str(r.get("kind") or KIND_UNKNOWN) == KIND_UNKNOWN for r in owed):
            lines.append("NOT SORTED means nobody has said whether it is a request, a standing "
                         "rule, background or a question: sort it with update_ask (kind).")
    if rules:
        lines += ["", "Standing rules in force (follow them in everything you do):"]
        lines += capped((rule_entry(r) for r in rules), MAX_RULES)
    if decisions:
        lines += ["", "Already waiting on a person (do not raise these again):"]
        lines += capped((waiting_entry(r) for r in decisions), MAX_WAITING)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars - 3].rstrip() + "..."
