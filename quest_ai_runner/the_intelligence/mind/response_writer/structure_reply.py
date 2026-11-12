"""Structure in a reply: what the open quest's structure adds to ONE reply, decided by code from the planner's structured ``structure_move`` and the records.

The planner names the move (``none`` by default; the stay-out turns never reach here). This reads the records of the one open quest through the host's
``structure_records`` port (``read(subject) -> {table: {id: row}}``), lets the hub's Structure in Conversation arm build the block from them, and then:

* ``frame`` and ``compare``: the reply is written as usual, and ONE small second call writes the closing paragraph (what it serves, the alternatives, the metric, the next
  milestone) and the one offer from the held facts, which code appends (``append_structure``). A single call asked to do both ignored the paragraph on advice turns.
* ``review`` and ``focus``: the reply itself is written from the records, so the writer gets a directive (``directive_for``).

The block rides the result for a surface to show. Nothing here reads the words of the user or of the reply. A quest with exactly one solution uses it; with several,
none is guessed and no offer is made (so no solution is duplicated). The offer is only a sentence and a ``{pieces}`` note in the block: what it keeps is applied later,
on a yes, by the host's ``keep_structure`` through ensure-one edits. Generic: no org, product or deployment names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from hubofhubs.api import STEP_SUMMARIZE, StructureMove, answer_with_reasoning, missing, vocabulary_note

log = logging.getLogger("quest-ai-runner.structure_reply")

#: Reply shapes that stay plain whatever move the planner named (a clarifying question, a decline, a readback, a yes or no, a brief, a spoken or ask-first reply).
PLAIN_SHAPES = ("yes_no", "ask_first", "clarify", "brief", "readback", "decline", "spoken", "plan")
APPENDED = ("frame", "compare")
CALL = "answer.structure"
MAX_REPLY_CHARS = 3500


@dataclass
class StructureReply:
    block: Dict[str, Any]
    offer: Optional[Dict[str, Any]]
    directive: str
    move: str = "none"
    facts: str = ""
    pieces: Optional[List[str]] = None


def single_quest_id(meta: Optional[Mapping[str, Any]]) -> str:
    """The quest id of a turn bound to exactly one quest, else ''."""
    ids = [str(q) for q in ((meta or {}).get("quest_ids") or []) if q]
    return ids[0] if len(ids) == 1 else ""


def solution_choice(records: Mapping[str, Any]) -> str:
    """The solution this reply builds on: the one solution a quest holds, else '' (with several, nothing is guessed from the person's words)."""
    solutions = records.get("solutions") or {}
    return next(iter(solutions)) if len(solutions) == 1 else ""


def held_lines(block: Mapping[str, Any]) -> List[str]:
    """The held parts of a block as one plain line each, the facts a reply is written from."""
    lines = []
    if (block.get("problem") or {}).get("name"):
        lines.append(f"What stands in the way (the problem): {block['problem']['name']}")
    if (block.get("ideal") or {}).get("name"):
        lines.append(f"What good looks like (the ideal): {block['ideal']['name']}")
    if (block.get("solution") or {}).get("name"):
        lines.append(f"The approach already chosen (the solution): {block['solution']['name']}")
    names = [a["name"] for a in block.get("alternatives") or [] if a.get("name")]
    if names:
        lines.append("Alternatives already kept for it: " + "; ".join(names))
    metric = block.get("metric")
    if metric and metric.get("name"):
        reading = metric.get("reading")
        value = (f", latest value {reading['value']} as of {reading['as_of']}" + (f" ({reading['source']})" if reading.get("source") else "")) if reading else ", no value logged yet"
        lines.append(f"The metric already kept: {metric['name']}{value}")
    if (block.get("milestone") or {}).get("sentence"):
        lines.append(f"The next milestone: {block['milestone']['sentence']}")
    return lines


def candidate_lines(records: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    """When the quest holds several problems or ideals and none was matched to a solution, their names as candidates (a few of each), for the writer to pick the one this serves."""
    lines = []
    for table, key, label in (("problems", "problem", "The quest's problems (name the one this reply serves)"), ("ideals", "ideal", "The quest's ideals (name the one that fits)")):
        names = [str(row.get("name") or row.get("problem") or "") for row in (records.get(table) or {}).values()]
        if not block.get(key) and names:
            lines.append(f"{label}: " + "; ".join(n[:120] for n in names if n)[:700])
    return "\n".join(lines) + ("\n" if lines else "")


def facts_text(records: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    """What the quest holds, as facts a writer must use first and never add a value or a date to; its solutions when several; and the candidates when nothing was matched."""
    held = held_lines(block)
    text = ("What the quest already holds (use it first, exactly as written, and never invent a value or a date):\n- " + "\n- ".join(held) + "\n") if held else \
        "The quest holds none of this structure yet, so propose each piece briefly from what you know of the quest.\n"
    solutions = [str(row.get("name") or "") for row in (records.get("solutions") or {}).values() if row.get("name")]
    if len(solutions) > 1:
        text += "The quest holds these solutions (name the one this reply serves, if one does): " + "; ".join(solutions[:6]) + ".\n"
    return text + candidate_lines(records, block)


OFFER_WORDS = {"solution": "the approach", "alternatives": "these alternatives", "metric": "a metric to measure them by", "milestone": "a next milestone"}

TAIL_TASK = {"frame": ("Write ONE short paragraph of two or three sentences (not a list, not a heading, and do not repeat the reply) that ties the reply to this quest: which problem of "
                       "the quest it serves, what good looks like for the person, that the options in the reply are alternatives to compare, the one metric that would show which works "
                       "(the held one with its latest value and date; otherwise propose one in a few words) and the next milestone (the held one; otherwise propose a small one)."),
             "compare": ("Write ONE short paragraph of two sentences that says the options in the reply are alternatives for the same solution and names the one metric that would show "
                         "which works (the held one with its latest value and date; otherwise propose one).")}
REVIEW_PARAGRAPH = ("REQUIRED: answer from the records. Say what the metric's latest value is with the date it was read and from where, and where the next milestone stands against it. If no value "
                    "is logged, say so plainly and name the metric that would show it. Two or three sentences, no list. ")
FOCUS_PARAGRAPH = ("REQUIRED: keep the short support shape. In it, name the one problem that stands in the way (in the person's own terms) and make the one next step the next milestone. ")


def offer_instruction(pieces: List[str]) -> str:
    if not pieces:
        return "Make no offer to keep anything."
    return ("After the paragraph write ONE short sentence offering to keep " + ", ".join(OFFER_WORDS[p] for p in pieces) + " in the quest so the next conversation builds on it, and nothing after it. "
            "If the conversation shows the person already declined keeping this, write no offer.")


def directive_for(move: str, block: Mapping[str, Any], facts: str) -> str:
    """The reply directive of a move whose reply is itself written from the records (review, focus): the held structure as facts, what to write, the words to use."""
    paragraph = {"review": REVIEW_PARAGRAPH, "focus": FOCUS_PARAGRAPH}[move]
    return "--- STRUCTURE FOR THIS REPLY (from the quest's own records) ---\n" + facts + paragraph + vocabulary_note()


def tail_prompt(move: str, reply: str, facts: str, pieces: List[str]) -> str:
    """The prompt of the closing paragraph: the reply as written, the held facts, the task of the move, the one offer, the words to use."""
    return ("You write the closing part of a reply a person will read in their own quest. The reply so far:\n\n" + reply[:MAX_REPLY_CHARS] + "\n\n" + facts + "\n" + TAIL_TASK[move] + " "
            + offer_instruction(pieces) + " " + vocabulary_note() + " Write only the closing text, in the voice of the reply, with no heading and no quotation marks.")


def structure_reply(plan: Any, meta: Optional[Mapping[str, Any]], records_source: Any, user_turn: bool) -> Optional[StructureReply]:
    """The structure of one reply, or None when the reply stays plain: no move, a plain shape, no single open quest, no records port or no records."""
    move = getattr(plan, "structure_move", None)
    if (not move or move == "none" or not user_turn or records_source is None or getattr(plan, "action", None) != "answer"
            or getattr(plan, "asks_first", False) or getattr(plan, "reply_shape", None) in PLAIN_SHAPES or getattr(plan, "delivery", None) == "spoken"):
        return None
    quest_id = single_quest_id(meta)
    if not quest_id:
        return None
    try:
        records = records_source.read(quest_id)
    except Exception as error:  # noqa: BLE001
        log.warning("structure records not read, reply stays plain: %s: %s", type(error).__name__, str(error)[:200])
        return None
    chosen = solution_choice(records)
    result = StructureMove().decide({"move": move, "records": records, "relevant_solution_ids": [chosen] if chosen else []})
    block = result.get("block")
    if not block:
        return None
    several = len(records.get("solutions") or {}) > 1
    pieces = [] if several else missing(block)     # with several solutions one of them may be the one this serves: never offer a second beside it
    can_write = getattr(records_source, "can_write", None)
    if pieces and callable(can_write):
        try:
            pieces = pieces if can_write(quest_id) else []      # the person's settings decide: an AI write needs the quest's autopilot on, so no offer that could not be kept
        except Exception as error:  # noqa: BLE001
            log.warning("structure write check failed, no offer: %s: %s", type(error).__name__, str(error)[:200])
            pieces = []
    block = dict(block, offer={"pieces": pieces} if pieces else None, solution_id=chosen or None)
    facts = facts_text(records, block)
    directive = "" if move in APPENDED else directive_for(move, block, facts)
    log.info("structure reply: move=%s held=%s offer=%s", move, len(held_lines(block)), (block["offer"] or {}).get("pieces"))
    return StructureReply(block=block, offer=block["offer"], directive=directive, move=move, facts=facts, pieces=pieces)


def append_structure(orch: Any, rs: Any, res: Any) -> bool:
    """Append the closing paragraph and offer of a ``frame`` or ``compare`` turn to its answer. True when something was added. A failed or empty call adds nothing: the reply
    stays the plain answer it was, never a guess."""
    structure = getattr(rs, "structure", None)
    if structure is None or structure.move not in APPENDED or getattr(res, "kind", "") != "answer" or not (res.text or "").strip():
        return False
    try:
        model = orch.registry.resolve_role(CALL)
        tail = answer_with_reasoning(orch.provider, [{"role": "user", "content": tail_prompt(structure.move, res.text, structure.facts, structure.pieces or [])}], model=model,
                                     step=STEP_SUMMARIZE, system="Answer exactly what is asked and nothing else. Never use em dashes.") or ""
    except Exception as error:  # noqa: BLE001 -- the structure must never lose the reply
        log.warning("structure closing paragraph skipped: %s: %s", type(error).__name__, str(error)[:200])
        return False
    tail = tail.replace("—", ",").strip()
    if not tail:
        return False
    res.text = res.text.rstrip() + "\n\n" + tail
    return True
