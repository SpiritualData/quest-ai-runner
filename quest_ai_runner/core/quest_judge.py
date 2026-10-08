"""Quest selection for a message: ONE structured judgment that returns the pick AND the ranking.

Selecting a quest is never done by word overlap alone. A consumer hands in its candidate quests in
the order it wants them considered (for example, priority order), and gets back:

  * ``chosen``: the quest the message is clearly about, or None when no listed quest is.
  * ``ranked``: EVERY candidate id, in order. The pick comes first when there is one, then the home
    quest, then the rest in the order given.

The judgment itself is an injected callable, so this module has no provider, tier, or database
dependency and any consumer can use it. Failure, an unknown id, or a missing verdict falls back to
the home quest (or None), never to a guess.
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from .card_filter import _extract_json

QUEST_SELECTION_TOOL: Dict[str, Any] = {
    "name": "quest_selection",
    "description": "Choose which quest, if any, the user's message is about.",
    "input_schema": {
        "type": "object",
        "properties": {
            "quest_id": {
                "type": "string",
                "description": "The id of the one quest the message is about, copied exactly from "
                               "the list, or the empty string when no listed quest is clearly it.",
            },
            "reason": {"type": "string", "description": "One short sentence: why."},
        },
        "required": ["quest_id"],
    },
}

QUEST_SELECTION_PROMPT = """\
Decide which ONE quest the user's latest message is about, or none.

Rules:
  * Judge by what the user is actually asking about, not by shared words. A word that happens to
    appear in a quest's state ("registration", "grant", "deadline") does not make the message about
    that quest.
  * The quest marked HOME is the one the user is working inside right now. It is the default: choose
    another quest only when the message clearly concerns that other quest's subject.
  * A short follow-up that names nothing ("and what is next for it?") continues the previous
    message's quest.
  * When no listed quest is clearly the subject, answer with an empty quest_id. A wrong quest puts
    unrelated state into the answer, which is worse than none.
  * Copy quest_id exactly from the list.

Do NOT use em dashes.

--- QUESTS ---
{quests}

--- PREVIOUS USER MESSAGE ---
{previous}

--- LATEST USER MESSAGE ---
{message}
"""


JUDGE_WINDOW = 25


def clip_text(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` chars keeping its START and its END (same cut as the
    orchestrator's clip_head_and_tail, which judges the newest words of a message too)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return text[:head].rstrip() + "\n[...]\n" + text[-tail:].lstrip()


def select_and_rank_quests(
    quests: List[Dict[str, str]],
    message: str,
    call_judge: Callable[[str, Dict[str, Any]], Any],
    *,
    previous_message: str = "",
    home_quest_id: Optional[str] = None,
    judge_limit: int = JUDGE_WINDOW,
) -> Tuple[Optional[str], List[str]]:
    """Return ``(chosen, ranked)`` for ``message`` over ``quests``.

    ``quests`` is ``[{"quest_id", "title", "state"}, ...]``; its order is the base ranking and is
    kept as given (the caller decides it). ``call_judge(prompt, tool_schema)`` makes the one LLM
    call and returns its verdict as a dict, or as a JSON string that is parsed here. It may raise;
    any failure is treated as no verdict. Never raises.

    ``ranked`` holds every candidate id: the pick first when there is one, otherwise the home quest
    first when it is known, then the rest in the order given.

    The judge sees only a window: the first ``judge_limit`` candidates in the given order, plus the
    home quest when it falls outside that window. The pick is only ever one the judge was shown, so
    ``ranked`` can cover every quest while the prompt stays small.
    """
    known = [q["quest_id"] for q in quests if q.get("quest_id")]
    if not known:
        return None, []
    home = home_quest_id if home_quest_id in known else None
    fallback = home

    def ordered(first: Optional[str]) -> List[str]:
        rest = [qid for qid in known if qid != first]
        return ([first] if first else []) + rest

    shown = [q for q in quests if q.get("quest_id")][:max(judge_limit, 1)]
    if home is not None and home not in {q["quest_id"] for q in shown}:
        shown.append(next(q for q in quests if q.get("quest_id") == home))
    shown_ids = {q["quest_id"] for q in shown}

    lines = []
    for q in shown:
        tag = " [HOME]" if q["quest_id"] == home_quest_id else ""
        state = " ".join((q.get("state") or "").split())[:240]
        lines.append(f"- {q['quest_id']}{tag}: {q.get('title') or '(untitled)'}"
                     + (f" | state: {state}" if state else ""))
    prompt = QUEST_SELECTION_PROMPT.format(
        quests="\n".join(lines),
        previous=clip_text(previous_message or "", 400) or "(none)",
        message=clip_text(message or "", 1000))

    try:
        result = call_judge(prompt, QUEST_SELECTION_TOOL)
        if isinstance(result, str):
            result = json.loads(_extract_json(result) or "{}")
    except Exception:  # noqa: BLE001 - selection must never break the caller
        return fallback, ordered(fallback)
    if not isinstance(result, dict) or "quest_id" not in result:
        return fallback, ordered(fallback)
    chosen = str(result.get("quest_id") or "").strip()
    if not chosen:
        return None, ordered(home)
    if chosen not in shown_ids:
        return fallback, ordered(fallback)
    return chosen, ordered(chosen)


def select_quest(
    quests: List[Dict[str, str]],
    message: str,
    call_judge: Callable[[str, Dict[str, Any]], Any],
    *,
    previous_message: str = "",
    home_quest_id: Optional[str] = None,
) -> Optional[str]:
    """Only the pick of :func:`select_and_rank_quests` (None when no listed quest is clearly it)."""
    chosen, _ranked = select_and_rank_quests(
        quests, message, call_judge,
        previous_message=previous_message, home_quest_id=home_quest_id)
    return chosen
