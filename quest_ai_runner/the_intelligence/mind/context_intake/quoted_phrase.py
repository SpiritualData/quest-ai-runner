"""A phrase the user quotes from the assistant's previous reply that matches no record, resolved to the closest word of that reply (a step of understanding for spoken turns).

Role: a voice transcript can turn a word of the assistant's own reply into another ("access" heard as "axis"). When the user asks about a phrase that is nowhere in the records
but sits one letter away from a word of the previous reply, the reply must tie it to that word and say what it is. One cheap model judgment sees both texts and either names
the phrase and the word it stands for or abstains; the answer is accepted only when the phrase is in the message and not in the previous reply and the word is in the previous
reply (a comparison of the three texts), and nothing else is changed.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_cue import parse_json

MAX_PHRASE = 40
PROMPT = (
    "The user's message was spoken and transcribed, so a word may have been heard wrongly.\n\nThe assistant's previous reply:\n{previous}\n\nThe user's message:\n{message}\n\n"
    "If the message asks about a phrase it quotes from that previous reply, and the phrase is not in the reply but is a mis-hearing of a word or phrase that is, return JSON "
    '{{"quoted":"the phrase as the message writes it","means":"the word or phrase of the previous reply it stands for, exactly as the reply writes it"}}. '
    "If the message quotes nothing from the previous reply, or you are not sure, return {{}}. Reply with the JSON only.")


def resolve(previous: str, message: str, decide: Callable[[str], str]) -> Dict[str, str]:
    """``{"quoted", "means", "sentence"}`` or ``{}``. The phrase must be in the message and not in the previous reply; the word it stands for must be in the previous reply."""
    if not (previous or "").strip() or not (message or "").strip():
        return {}
    try:
        data = parse_json(decide(PROMPT.format(previous=previous[:2500], message=message[:1500])))
    except Exception:  # noqa: BLE001 -- an unreadable judgment resolves nothing
        return {}
    quoted, means = str(data.get("quoted") or "").strip(), str(data.get("means") or "").strip()
    if not quoted or not means or len(quoted) > MAX_PHRASE or len(means) > MAX_PHRASE or quoted.lower() == means.lower():
        return {}
    if quoted.lower() not in message.lower() or quoted.lower() in previous.lower() or means.lower() not in previous.lower():
        return {}
    sentence = next((s.strip() for s in re.split(r"(?<=[.!?])\s+", previous) if means.lower() in s.lower()), "")
    return {"quoted": quoted, "means": means, "sentence": sentence}


def note(found: Dict[str, str]) -> str:
    """The block the understanding adds to the turn's context, or "" when nothing was resolved."""
    if not found:
        return ""
    return (f"--- THE USER'S PHRASE, RESOLVED (set by the system) ---\nThe user's \"{found['quoted']}\" was heard from your previous reply's \"{found['means']}\""
            + (f" (\"{found['sentence']}\")" if found.get("sentence") else "")
            + f". Answer as if they had said \"{found['means']}\", and say plainly what it is, in full, in the first sentence.")


def previous_reply(store: Any, conv_id: str, message: str) -> str:
    """The text of the last exchange of this conversation as the store keeps it, or ""."""
    try:
        slice_ = store.current_slice(conv_id, message, recent_turns=1)
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(slice_, "text", "") or "") if slice_ is not None else ""


def note_for(store: Any, conv_id: str, message: str, decide: Optional[Callable[[str], str]]) -> str:
    """The resolution note for this turn, or "" (no store, no earlier exchange, nothing quoted, or an abstention)."""
    if store is None or not conv_id or decide is None:
        return ""
    return note(resolve(previous_reply(store, conv_id, message), message, decide))
