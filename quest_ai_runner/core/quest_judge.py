"""Quest ranking and selection: the ONE semantic ranker for every quest-selection use case.

One LLM call ranks the candidate quests for one or more messages. For each message it returns the
quest the message is clearly about (or none) and the relevance order of the candidates. A single
message is a batch of one, so the prompt, the tool schema, the validation and the fallbacks are the
same code in every case (chat turns, reflection tasks, the AI task form, reflection batches).

Priority is NOT the ranking. It is only the fallback order used when the call fails or its answer is
not usable for a message. The consumer supplies that fallback order and the candidate list; this
module never reads a database.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .card_filter import _extract_json

log = logging.getLogger("quest-ai-runner.quest_judge")

JUDGE_WINDOW = 25

QUEST_RANKING_TOOL: Dict[str, Any] = {
    "name": "quest_ranking",
    "description": "For each numbered message, pick the quest it is about (or none) and rank the "
                   "listed quests by relevance to that message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "integer",
                                 "description": "The number of the message this answers."},
                        "quest_id": {"type": "string",
                                     "description": "The one quest the message is clearly about, "
                                                    "copied exactly from the list, or the empty "
                                                    "string when no listed quest is clearly it."},
                        "ranking": {"type": "array", "items": {"type": "string"},
                                    "description": "Every listed quest_id, once each, most relevant "
                                                   "to this message first."},
                    },
                    "required": ["item", "quest_id", "ranking"],
                },
            },
        },
        "required": ["results"],
    },
}

QUEST_RANKING_PROMPT = """\
For each numbered message below, decide which ONE quest it is about (or none), and rank the listed
quests by how relevant each one is to that message.

Rules:
  * Judge by what each message is actually asking about, not by shared words. A word that happens to
    appear in a quest's state ("registration", "grant", "deadline") does not make the message about
    that quest.
  * The quest marked HOME is the one the user is working inside right now. It is the default: choose
    another quest only when the message clearly concerns that other quest's subject.
  * A short follow-up that names nothing ("and what is next for it?") continues the PREVIOUS message's
    quest, when a PREVIOUS message is given.
  * When no listed quest is clearly the subject, answer with an empty quest_id. A wrong quest puts
    unrelated state into the answer, which is worse than none.
  * Copy quest_id exactly from the list. Each ranking lists every quest id shown, once each.

Do NOT use em dashes.

--- QUESTS ---
{quests}

--- MESSAGES ---
{messages}
"""


def answer_format_instruction() -> str:
    """The JSON answer shape for a providers that take no tool schema, derived from
    ``QUEST_RANKING_TOOL`` so the shape is defined once."""
    row = QUEST_RANKING_TOOL["input_schema"]["properties"]["results"]["items"]["properties"]
    example = {"results": [{"item": 1, "quest_id": "<id or empty string>", "ranking": ["<id>"]}]}
    return ("Answer with ONE JSON object only, no prose, no markdown fences, with one entry per "
            f"numbered message ({', '.join(row)}): " + json.dumps(example)
            + " Every ranking lists each shown quest id once, most relevant first.")


def clip_text(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` chars keeping its START and its END (the newest words of a
    message matter as much as its opening)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return text[:head].rstrip() + "\n[...]\n" + text[-tail:].lstrip()


STATE_LIMIT = 320


def tail_clip(text: str, limit: int) -> str:
    """The LAST ``limit`` chars of ``text``, with a leading marker when cut. The newest words of a
    quest's state are the ones that say where it stands now, so the start is what gets dropped."""
    if len(text) <= limit:
        return text
    return "[...] " + text[-limit:].lstrip()


def _quest_lines(shown: List[Dict[str, str]], home_quest_id: Optional[str]) -> str:
    lines = []
    for q in shown:
        tag = " [HOME]" if q["quest_id"] == home_quest_id else ""
        state = tail_clip(" ".join((q.get("state") or "").split()), STATE_LIMIT)
        lines.append(f"- {q['quest_id']}{tag}: {q.get('title') or '(untitled)'}"
                     + (f" | state: {state}" if state else ""))
    return "\n".join(lines)


def _message_blocks(items: List[Dict[str, str]]) -> str:
    blocks = []
    for n, item in enumerate(items, start=1):
        previous = clip_text(item.get("previous") or "", 400) or "(none)"
        message = clip_text(item.get("text") or "", 1000)
        blocks.append(f"[{n}] PREVIOUS: {previous}\nMESSAGE: {message}")
    return "\n\n".join(blocks)


def _parse_results(raw: Any, count: int) -> Dict[int, Dict[str, Any]]:
    """The verdict rows keyed by 0-based message index. Anything malformed is simply absent."""
    if isinstance(raw, str):
        raw = json.loads(_extract_json(raw) or "{}")
    if not isinstance(raw, dict):
        return {}
    rows: Dict[int, Dict[str, Any]] = {}
    for row in raw.get("results") or []:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("item")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < count:
            rows[index] = row
    return rows


def rank_quests(
    items: List[Dict[str, str]],
    quests: List[Dict[str, str]],
    call_judge: Callable[[str, Dict[str, Any]], Any],
    *,
    home_quest_id: Optional[str] = None,
    judge_limit: int = JUDGE_WINDOW,
    fallback_order: Optional[List[str]] = None,
) -> List[Tuple[Optional[str], List[str]]]:
    """Rank ``quests`` for every message in ``items`` in ONE call. Never raises.

    ``items`` is ``[{"text": str, "previous": str}, ...]``; the result is aligned with it, one
    ``(chosen, ranked)`` per message. ``quests`` is ``[{"quest_id", "title", "state"}, ...]`` in the
    order the caller wants the judge window filled from (recency, for example).

    ``chosen`` is the quest the message is clearly about, or None. ``ranked`` lists EVERY quest id,
    most relevant first. The judge is shown only the first ``judge_limit`` quests (plus the home
    quest when it falls outside that window); anything it did not rank is appended in
    ``fallback_order``. ``fallback_order`` (priority, for example) is also used whole when a message
    gets no usable answer; by default it is the given order.

    ``call_judge(prompt, tool_schema)`` makes the one LLM call and returns its verdict as a dict, or
    as a JSON string. It may raise; a failure counts as no verdict for every message.
    """
    known = [q["quest_id"] for q in quests if q.get("quest_id")]
    if not items or not known:
        return [(None, []) for _ in items]
    home = home_quest_id if home_quest_id in known else None
    base = [qid for qid in (fallback_order or known) if qid in known]
    base += [qid for qid in known if qid not in base]

    def fallback_for() -> Tuple[Optional[str], List[str]]:
        return home, ([home] if home else []) + [qid for qid in base if qid != home]

    shown = [q for q in quests if q.get("quest_id")][:max(judge_limit, 1)]
    if home is not None and home not in {q["quest_id"] for q in shown}:
        shown.append(next(q for q in quests if q.get("quest_id") == home))
    shown_ids = {q["quest_id"] for q in shown}

    prompt = QUEST_RANKING_PROMPT.format(
        quests=_quest_lines(shown, home_quest_id),
        messages=_message_blocks(items))
    try:
        rows = _parse_results(call_judge(prompt, QUEST_RANKING_TOOL), len(items))
    except Exception as exc:  # noqa: BLE001 - ranking must never break the caller
        log.warning("quest ranking failed for %d message(s), using fallback order: %s", len(items), exc)
        return [fallback_for() for _ in range(len(items))]

    results: List[Tuple[Optional[str], List[str]]] = []
    for index in range(len(items)):
        row = rows.get(index)
        if row is None:
            results.append(fallback_for())
            continue
        ranked: List[str] = []
        for qid in row.get("ranking") or []:
            qid = str(qid).strip()
            if qid in shown_ids and qid not in ranked:
                ranked.append(qid)
        ranked += [qid for qid in base if qid not in ranked]
        chosen_raw = str(row.get("quest_id") or "").strip()
        if not chosen_raw:
            chosen: Optional[str] = None
        elif chosen_raw in shown_ids:
            chosen = chosen_raw
        else:
            results.append(fallback_for())
            continue
        if chosen is not None:
            ranked = [chosen] + [qid for qid in ranked if qid != chosen]
        results.append((chosen, ranked))
    return results


def select_and_rank_quests(
    quests: List[Dict[str, str]],
    message: str,
    call_judge: Callable[[str, Dict[str, Any]], Any],
    *,
    previous_message: str = "",
    home_quest_id: Optional[str] = None,
    judge_limit: int = JUDGE_WINDOW,
    fallback_order: Optional[List[str]] = None,
) -> Tuple[Optional[str], List[str]]:
    """One message: the batch of one. Returns ``(chosen, ranked)``. Never raises."""
    return rank_quests(
        [{"text": message, "previous": previous_message}], quests, call_judge,
        home_quest_id=home_quest_id, judge_limit=judge_limit, fallback_order=fallback_order)[0]


def select_quest(
    quests: List[Dict[str, str]],
    message: str,
    call_judge: Callable[[str, Dict[str, Any]], Any],
    *,
    previous_message: str = "",
    home_quest_id: Optional[str] = None,
) -> Optional[str]:
    """Only the pick of :func:`select_and_rank_quests`."""
    chosen, _ranked = select_and_rank_quests(
        quests, message, call_judge,
        previous_message=previous_message, home_quest_id=home_quest_id)
    return chosen
