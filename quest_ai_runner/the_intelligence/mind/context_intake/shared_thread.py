"""What a reply that every participant of a shared thread reads may draw on.

A turn's context assembly selects the asker's records without knowing who will read the reply. In a shared thread only a record the
consumer marked visible to every participant (``visible_to_all`` on the card's metadata, or its id in ``context_meta["visible_card_ids"]``)
may be quoted or paraphrased; the asker's other records stay out of the context the reply is written from, and a reply sentence that
still draws on one is dropped. Everything here compares STORED card text with the reply (the overlap test the work trace uses to decide
whether a reply draws on a record); it reads no meaning out of the reply and gates on no keyword.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_intelligence.mind.response_writer import work_trace

SHARED_THREAD_RULE = (
    "--- SHARED THREAD (INTERNAL) ---\nEvery participant of this conversation reads this reply, not only the person who asked. Use only "
    "what the request itself says, what the conversation shows to all of them and records marked visible to everyone. The asker's "
    "other records are not part of this context: do not state, hint at or build on details of anyone's private records.")


def audience_of(meta: Optional[Mapping[str, Any]]) -> str:
    meta = meta or {}
    setting = meta.get("work_trace") if isinstance(meta.get("work_trace"), Mapping) else {}
    return str(meta.get("audience") or setting.get("audience") or "").strip().lower()


def is_shared_thread(meta: Optional[Mapping[str, Any]]) -> bool:
    """True when the consumer said the reply is read by the whole thread (audience ``shared``)."""
    return audience_of(meta) == "shared"


def visible_card_ids(meta: Optional[Mapping[str, Any]]) -> set:
    return {work_trace.canonical_id(i) for i in ((meta or {}).get("visible_card_ids") or [])}


def private_cards(card_metadata: Sequence[Any], meta: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """The selected cards a shared-thread reply must not draw on: ``{"title", "text", "section"}`` for each card that is neither marked
    ``visible_to_all`` nor listed in ``visible_card_ids``. A conversation-history card is the thread's own and is left out of the list."""
    visible = visible_card_ids(meta)
    found: List[Dict[str, str]] = []
    for card in card_metadata or []:
        if not isinstance(card, Mapping) or card.get("visible_to_all") is True:
            continue
        if work_trace.canonical_id(card.get("id")) in visible or str(card.get("adapter") or "").lower() == work_trace.HISTORY_ADAPTER:
            continue
        section = str(card.get("rendered_section") or "")
        parts = [section] + [str(i.get("text") or "") for i in (card.get("items") or []) if isinstance(i, Mapping)]
        text = "\n".join(p for p in parts if p)[:work_trace.MAX_CARD_TEXT]
        title = str(card.get("name") or card.get("title") or "")
        if text.strip():
            found.append({"title": title, "text": text, "section": section})
    return found


def strip_private_sections(context_view: str, private: Sequence[Mapping[str, str]]) -> str:
    """``context_view`` without the rendered section of each private card (the section is a verbatim part of the view)."""
    view = context_view or ""
    for card in private:
        section = card.get("section") or ""
        if section and section in view:
            view = view.replace(section, "")
    return view


def scrub_private_sentences(text: str, private: Sequence[Mapping[str, str]]) -> str:
    """``text`` without the sentences that draw on a private card: the card's title stands in the sentence, a run of its words does, or the
    sentence shares three distinct rare words or numbers with the card's stored text. Unchanged when nothing overlaps."""
    if not (text or "").strip() or not private:
        return text
    kept: List[str] = []
    changed = False
    for line in str(text).split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", line)
        keep = [s for s in sentences if not any(work_trace.reply_draws_on({"title": c.get("title"), "text": c.get("text")}, [], s) for c in private)]
        changed = changed or len(keep) != len(sentences)
        kept.append(" ".join(keep))
    return "\n".join(kept).strip() if changed else text
