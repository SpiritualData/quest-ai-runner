"""The asks waiting on a person, as one retrievable card per quest.

A question about what is blocking a quest, what waits on a person or what still needs a decision is answered from the quest's open decision-requests. They are not part of the quest's
folder card (which is always offered for a matched quest) and must not be: a question about a goal's due date or what a feature does must bring no ask into the context. So the open
asks are their own card, ``quest-asks-<quest id>``: it competes with every other card on relevance to the request (the store's selection decides, never a word list here), it is
fenced to its quest by ``scope_tags``, and the quest's priority list never names it. The card's words come from the records (the kinds, who each ask waits on, when it was raised), not from
any request.

A read that failed leaves the existing card untouched (``None`` from the client, never "nothing is waiting"); a read that found nothing open writes a card that says so, so "nothing waits on
anyone" is a fact read at a time and not an absence. The card says when it was read. Refreshing is throttled per quest in this process and costs one read."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.quest-ask-cards")

ASKS_CARD_PREFIX = "quest-asks-"
ASKS_CARD_OWNER = "quest_open_asks"
REFRESH_SECONDS = 120.0
RESTAMP_SECONDS = 900.0       # an unchanged set of asks is not rewritten more often than this (the card says when it was read)
MAX_ASKS_IN_CARD = 25
ASKS_SOURCE_WHAT = "the quest's open asks and decisions waiting on a person: what is asked, of whom, what kind (blocker, approval, something to do, a clarification), and since when"
SUMMARY_CHARS = 300
last_refresh: Dict[str, float] = {}
refresh_lock = threading.Lock()


def asks_card_id(quest_id: str) -> str:
    return ASKS_CARD_PREFIX + quest_id


def ask_line(decision: Dict[str, Any]) -> str:
    """One open ask as a plain line: what is asked, of whom, what kind, how old, whether it is overdue."""
    summary = " ".join(str(decision.get("summary") or "(no summary)").split())[:SUMMARY_CHARS]
    who = str(decision.get("assignee_name") or "").strip()
    kind = str(decision.get("kind") or "").strip()
    raised = str(decision.get("created_at") or "")[:10]
    bits = [f"waiting on {who}" if who else "", kind, f"raised {raised}" if raised else "", "OVERDUE" if decision.get("overdue") else ""]
    return f"- {summary} ({', '.join(b for b in bits if b)})"


def asks_card(quest_id: str, title: str, decisions: Sequence[Dict[str, Any]], read_at: Optional[datetime] = None) -> Dict[str, Any]:
    """The card of a quest's open asks. ``decisions`` are the OPEN ones (possibly none)."""
    from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.recent_context import quest_scope_key
    stamp = (read_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    shown = list(decisions)[:MAX_ASKS_IN_CARD]
    who = sorted({str(d.get("assignee_name") or "").strip() for d in decisions if str(d.get("assignee_name") or "").strip()})
    kinds = sorted({str(d.get("kind") or "").strip() for d in decisions if str(d.get("kind") or "").strip()})
    name = f"Open asks waiting on a person: {title}" if title else f"Open asks waiting on a person: quest {quest_id}"
    if decisions:
        summary = (f"{len(decisions)} open ask{'s' if len(decisions) != 1 else ''} on this quest, waiting on {', '.join(who) or 'people'}"
                   f" (blockers, approvals, things to do, clarifications). Read {stamp}.")
        body = "\n".join([f"Open asks on this quest, read {stamp} ({len(decisions)} in all; each is waiting on the person named):"] + [ask_line(d) for d in shown]
                         + ([f"... and {len(decisions) - len(shown)} more"] if len(decisions) > len(shown) else []))
    else:
        summary = f"No open asks on this quest: nothing is waiting on a person. Read {stamp}."
        body = f"No decision-request on this quest is open (read {stamp}): nothing is waiting on a person."
    return {
        "id": asks_card_id(quest_id),
        "name": name,
        "summary": summary,
        "description": summary,
        "keywords": sorted({"ask", "asks", "decision", "decisions", "waiting", *kinds, *(w.lower() for w in who)}),
        "conventions": [],
        "files": [],
        "content": [{"id": "open-asks", "type": "note", "why": "The quest's open asks and who each waits on", "locator": {"text": body}}],
        "scope_tags": [quest_scope_key(quest_id)],
        "managed_by": ASKS_CARD_OWNER,
        "managed_fields": ["name", "summary", "description", "keywords", "files"],
        "managed_items": ["open-asks"],
        "provenance": {"created_by_task": "open asks sync", "model": "", "created_at": "", "last_verified_at": ""},
    }


def sync_open_asks_card(store: Any, client: Any, quest_id: str, title: str = "", now: Optional[float] = None, force: bool = False) -> bool:
    """Read the quest's open asks and write its card when it changed. Returns True when a card was written. A failed read, no store or no client changes nothing."""
    if store is None or client is None or not quest_id:
        return False
    moment = time.monotonic() if now is None else now
    with refresh_lock:
        if not force and moment - last_refresh.get(quest_id, -1e9) < REFRESH_SECONDS:
            return False
        last_refresh[quest_id] = moment
    reader = getattr(client, "open_decisions_or_none", None)
    if not callable(reader):
        return False
    decisions = reader(quest_id)
    if decisions is None:
        log.info("open asks of %s could not be read; the card is left as it was", quest_id, extra={"in_transcript": False})
        return False
    card = asks_card(quest_id, title, decisions)
    digest = "\n".join(ask_line(d) for d in decisions)
    try:
        read = getattr(store, "get_listed_card", None) or store.get_card
        existing = read(card["id"]) or {}
    except Exception:  # noqa: BLE001 -- an unreadable existing card is rewritten
        existing = {}
    if existing and existing.get("ask_digest") == digest and time.time() - float(existing.get("read_epoch") or 0.0) < RESTAMP_SECONDS:
        return False      # the same asks, read a moment ago: the card already says it
    card.update(usage_count=existing.get("usage_count", 0), ask_digest=digest, read_epoch=time.time())
    return bool(store.write_card(card["id"], card))
