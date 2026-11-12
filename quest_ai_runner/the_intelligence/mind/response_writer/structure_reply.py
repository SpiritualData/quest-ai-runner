"""Structure in a reply: what the open quest's structure adds to ONE reply, decided by code from the planner's structured ``structure_move`` and the records.

The planner names the move (``none`` by default; the stay-out turns never reach here). This reads the records of the one open quest through the host's
``structure_records`` port (``read(subject) -> {table: {id: row}}``), picks the solution the question serves (one solution is used; with several the hub's Relevant Solutions
arm, a records shortlist then one fast-tier classification, picks one or says none fits), lets the hub's Structure in Conversation arm build the block from the records, and then:

* ``frame`` and ``compare``: the reply is written as usual and the closing paragraph (what it serves, the alternatives, the metric, the next milestone) and the one offer are
  appended by code from the held facts, by one small second call (``append_structure``), or asked of the single answer call (``OrchestratorConfig.structure_in_one_call``).
* ``review`` and ``focus``: the reply itself is written from the records. When the turn is a status or next-step snapshot (the one home of those turns) the structure facts
  are fed into that snapshot reply and nothing else is written.

The offer is parked on the port with a key (``park_offer``); the next turn shows the planner an open offer, the planner reports ``accepts_offer`` or ``declines_offer`` as
structured fields, and CODE keeps (``accept_offer``) or remembers the decline (``decline_offer``). Nothing here reads the words of the user or of the reply. Generic: no org,
product or deployment names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from hubofhubs.api import STEP_SUMMARIZE, RecordsThenModel, SolutionNetwork, StructureMove, answer_with_reasoning, missing, vocabulary_note

log = logging.getLogger("quest-ai-runner.structure_reply")

#: Reply shapes that stay plain whatever move the planner named (a clarifying question, a decline, a readback, a yes or no, a brief, a spoken or ask-first reply).
PLAIN_SHAPES = ("yes_no", "ask_first", "clarify", "brief", "readback", "decline", "spoken", "plan")
APPENDED = ("frame", "compare")
CALL = "answer.structure"
MAX_REPLY_CHARS = 3500
KEYED = {"problems": "problem_id", "ideals": "ideal_id", "solutions": "solution_id", "implementations": "implementation_id", "metrics": "metric_id"}


@dataclass
class StructureReply:
    block: Dict[str, Any]
    offer: Optional[Dict[str, Any]]
    directive: str
    move: str = "none"
    facts: str = ""
    pieces: Optional[List[str]] = None
    quest_id: str = ""
    one_call: bool = False
    snapshot: bool = False


def single_quest_id(meta: Optional[Mapping[str, Any]]) -> str:
    """The quest id of a turn bound to exactly one quest, else ''."""
    ids = [str(q) for q in ((meta or {}).get("quest_ids") or []) if q]
    return ids[0] if len(ids) == 1 else ""


def cell(value: Any) -> str:
    return ";".join(str(v) for v in value) if isinstance(value, (list, tuple)) else ("" if value is None else str(value))


def rows_of(records: Mapping[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """The quest's records as the rows the hub's relevance network reads (``{table: [row]}``, a record id in each row's own id column, lists as semicolon text)."""
    return {table: [dict({key: cell(value) for key, value in row.items()}, **{column: str(record_id)}) for record_id, row in (records.get(table) or {}).items()]
            for table, column in KEYED.items()}


class Plumbing:
    """The one-argument model call of the turn as the hub's Relevant Solutions arm asks for it (``ask(prompt, tier)``)."""

    def __init__(self, ask: Callable[[str], str]):
        self.ask_once = ask

    def ask(self, prompt: str, tier: str = "fast") -> str:
        return self.ask_once(prompt)


def solution_choice(records: Mapping[str, Any], message: str = "", ask: Optional[Callable[[str], str]] = None) -> str:
    """The solution this reply serves: the one solution a quest holds, else the one the hub's Relevant Solutions arm (the records' shortlist, then one fast-tier call over each
    candidate's name and purpose) says the question concerns, else '' (none fits, or nothing could be asked)."""
    solutions = records.get("solutions") or {}
    if len(solutions) == 1:
        return next(iter(solutions))
    if len(solutions) < 2 or not message or ask is None:
        return ""
    try:
        hits = RecordsThenModel(Plumbing(ask), "fast").relevant(message, SolutionNetwork.from_rows(rows_of(records)), 1).get("solutions") or []
    except Exception as error:  # noqa: BLE001 -- a failed match is "none fits", never a failed turn
        log.warning("relevant solution not chosen, none fits: %s: %s", type(error).__name__, str(error)[:200])
        return ""
    return str(hits[0]["solution_id"]) if hits and str(hits[0]["solution_id"]) in solutions else ""


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
        lines.append(f"The metric already kept (never propose another): {metric['name']}{value}")
    if (block.get("milestone") or {}).get("sentence"):
        lines.append(f"The next milestone: {block['milestone']['sentence']}")
    return lines


def candidate_lines(records: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    """When none of the quest's solutions fits and the quest holds several problems or ideals, their names as candidates (a few of each), for the writer to pick the one this serves."""
    lines = []
    for table, key, label in (("problems", "problem", "The quest's problems (name the one this reply serves)"), ("ideals", "ideal", "The quest's ideals (name the one that fits)")):
        names = [str(row.get("name") or row.get("problem") or "") for row in (records.get(table) or {}).values()]
        if not block.get(key) and names:
            lines.append(f"{label}: " + "; ".join(n[:120] for n in names if n)[:700])
    return "\n".join(lines) + ("\n" if lines else "")


def facts_text(records: Mapping[str, Any], block: Mapping[str, Any], matched: bool) -> str:
    """What the quest holds for this question, as facts a writer must use first and never add a value or a date to; the candidate problems and ideals when none was matched."""
    held = held_lines(block)
    text = ("What the quest already holds for this question (use it first, exactly as written, and never invent a value or a date):\n- " + "\n- ".join(held) + "\n") if held else \
        "The quest holds none of this structure yet, so propose each piece briefly from what you know of the quest.\n"
    if not matched and len(records.get("solutions") or {}) > 1:
        text += "None of the quest's solutions is about this question, so a new one would be proposed.\n"
    return text + (candidate_lines(records, block) if not matched else "")


OFFER_WORDS = {"solution": "the approach", "alternatives": "these alternatives", "metric": "a metric to measure them by", "milestone": "a next milestone"}

TAIL_TASK = {"frame": ("Write ONE short paragraph of two or three sentences (not a list, not a heading, and do not repeat the reply) that ties the reply to this quest: which problem of "
                       "the quest it serves, what good looks like for the person, that the options in the reply are alternatives to compare, the one metric that would show which works "
                       "and the next milestone. Use every held fact above as it is written, with the metric's latest value and date when one is given; a held metric is never replaced "
                       "by a new one, and only a piece the facts do not hold is proposed, in a few words."),
             "compare": ("Write ONE short paragraph of two sentences that says the options in the reply are alternatives for the same solution and names the one metric that would show "
                         "which works. Use the held metric with its latest value and date when one is given and never propose another; propose one only when none is held.")}
REVIEW_PARAGRAPH = ("REQUIRED: answer from the records. Say what the metric's latest value is with the date it was read and from where, and where the next milestone stands against it. If no value "
                    "is logged, say so plainly and name the metric that would show it. Two or three sentences, no list. ")
FOCUS_PARAGRAPH = ("REQUIRED: keep the short support shape. In it, name the one problem that stands in the way (in the person's own terms) and make the one next step the next milestone. ")
ONE_CALL_PARAGRAPH = ("REQUIRED, and it overrides any instruction above to give the answer alone: after the answer, add one more short paragraph of two or three sentences (not a list, not a "
                      "heading, and do not repeat the list) that ties the answer to this quest: which problem of the quest it serves, what good looks like for the person, that the options "
                      "above are alternatives to compare, the one metric that would show which works and the next milestone, using the held facts as written (never a new metric when one "
                      "is held). ")


def offer_instruction(pieces: List[str]) -> str:
    if not pieces:
        return "Make no offer to keep anything."
    return ("After the paragraph write ONE short sentence offering to keep " + ", ".join(OFFER_WORDS[p] for p in pieces) + " in the quest so the next conversation builds on it, and nothing after it. "
            "If the conversation shows the person already declined keeping this, write no offer.")


def directive_for(move: str, facts: str, snapshot: bool, one_call: bool, pieces: List[str]) -> str:
    """The reply directive when the writer itself writes from the facts: a review or focus reply, a snapshot reply, or a framed or compared reply asked of one call."""
    head = "--- STRUCTURE FOR THIS REPLY (from the quest's own records) ---\n" + facts
    if snapshot:
        return ""      # a snapshot reply is composed by code and model calls of its own; the structure joins it as one code-written sentence (``snapshot_sentence``)
    if move in APPENDED:
        return (head + ONE_CALL_PARAGRAPH + offer_instruction(pieces) + " " + vocabulary_note()) if one_call else ""
    return head + {"review": REVIEW_PARAGRAPH, "focus": FOCUS_PARAGRAPH}[move] + vocabulary_note()


def tail_prompt(move: str, reply: str, facts: str, pieces: List[str]) -> str:
    """The prompt of the closing paragraph: the reply as written, the held facts, the task of the move, the one offer, the words to use."""
    return ("You write the closing part of a reply a person will read in their own quest. The reply so far:\n\n" + reply[:MAX_REPLY_CHARS] + "\n\n" + facts + "\n" + TAIL_TASK[move] + " "
            + offer_instruction(pieces) + " " + vocabulary_note() + " Write only the closing text, in the voice of the reply, with no heading and no quotation marks.")


def snapshot_sentence(move: str, block: Mapping[str, Any]) -> str:
    """The structure of a status or next-step snapshot turn, in plain sentences composed by code from the block (no model, no value not in the records): for a review the
    metric the solution is measured by with its latest value and date (or that none is logged yet) and the next milestone; for a focus the problem standing in the way and
    the next milestone. '' when the block holds none of it."""
    parts: List[str] = []
    if move == "review":
        metric = block.get("metric") or {}
        if metric.get("name"):
            reading = metric.get("reading")
            parts.append((f"The metric that shows whether this is working is {metric['name']}, last recorded as {reading['value']} on {reading['as_of']}." if reading
                          else f"The metric that shows whether this is working is {metric['name']}, and no value is logged for it yet."))
    elif (block.get("problem") or {}).get("name"):
        parts.append(f"The problem standing in your way is: {block['problem']['name']}.")
    if (block.get("milestone") or {}).get("sentence"):
        parts.append(f"The next milestone is {block['milestone']['sentence'].rstrip('.')}.")
    return " ".join(parts)


def offer_key(chosen: str, pieces: List[str]) -> str:
    return "structure:" + (chosen or "new") + ":" + ",".join(pieces)


def structure_reply(plan: Any, meta: Optional[Mapping[str, Any]], records_source: Any, user_turn: bool, message: str = "", ask: Optional[Callable[[str], str]] = None,
                    snapshot: bool = False, one_call: bool = False) -> Optional[StructureReply]:
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
    chosen = solution_choice(records, message, ask)
    result = StructureMove().decide({"move": move, "records": records, "relevant_solution_ids": [chosen] if chosen else []})
    block = result.get("block")
    if not block:
        return None
    pieces = missing(block)
    mode = ""
    if pieces:
        try:
            mode = str(records_source.write_mode(quest_id) or "")      # the person's settings and the quest's autopilot decide: "auto", "ask_first", or "" when the AI may not write
            if mode not in ("auto", "ask_first"):
                pieces = []
            elif offer_key(chosen, pieces) in set(records_source.declined(quest_id) or ()):
                pieces = []      # a declined offer stands, in every conversation
        except Exception as error:  # noqa: BLE001
            log.warning("structure write check failed, no offer: %s: %s", type(error).__name__, str(error)[:200])
            pieces = []
    key = offer_key(chosen, pieces) if pieces else ""
    block = dict(block, offer={"key": key, "pieces": pieces, "mode": mode} if pieces else None, solution_id=chosen or None)
    facts = facts_text(records, block, bool(chosen))
    directive = directive_for(move, facts, snapshot, one_call, pieces)
    log.info("structure reply: move=%s held=%s offer=%s snapshot=%s", move, len(held_lines(block)), pieces, snapshot)
    return StructureReply(block=block, offer=block["offer"], directive=directive, move=move, facts=facts, pieces=pieces, quest_id=quest_id, one_call=one_call and not snapshot,
                          snapshot=snapshot)


def finish_structure(orch: Any, rs: Any, res: Any) -> bool:
    """After the reply is final: append the closing paragraph and offer of a ``frame`` or ``compare`` turn (unless the single answer call wrote them), then park the offer with its
    key and the reply it came from. True when text was appended. A failed or empty call adds nothing: the reply stays the plain answer it was, never a guess."""
    structure = getattr(rs, "structure", None)
    if structure is None or getattr(res, "kind", "") != "answer" or not (res.text or "").strip():
        return False
    added = False
    if structure.snapshot:
        sentence = snapshot_sentence(structure.move, structure.block)
        if sentence:
            res.text = res.text.rstrip() + " " + sentence
            added = True
        return added
    if structure.move in APPENDED and not structure.one_call and not getattr(rs, "alternatives_kept", False):
        try:
            model = orch.registry.resolve_role(CALL)
            tail = answer_with_reasoning(orch.provider, [{"role": "user", "content": tail_prompt(structure.move, res.text, structure.facts, structure.pieces or [])}], model=model,
                                         step=STEP_SUMMARIZE, system="Answer exactly what is asked and nothing else. Never use em dashes.") or ""
        except Exception as error:  # noqa: BLE001 -- the structure must never lose the reply
            log.warning("structure closing paragraph skipped: %s: %s", type(error).__name__, str(error)[:200])
            tail = ""
        tail = tail.replace("—", ",").strip()
        if tail:
            res.text = res.text.rstrip() + "\n\n" + tail
            added = True
    if structure.offer:
        try:
            ids = (getattr(rs, "_ctx_meta", None) or {})
            parked = orch.cfg.structure_records.park_offer(structure.quest_id, str(ids.get("conversation_id") or ""),
                                                           dict(structure.offer, solution_id=structure.block.get("solution_id") or ""), res.text)
            structure.block["offer"] = dict(structure.offer, id=parked)
        except Exception as error:  # noqa: BLE001 -- not parked: the reply still stands, and the offer cannot be accepted
            log.warning("structure offer not parked: %s: %s", type(error).__name__, str(error)[:200])
    return added


OPEN_NOTE = ("--- AN OFFER IS OPEN (your previous reply offered to keep structure in the quest) ---\nIt offered to keep: {pieces}. Set `accepts_offer` true when the person's message "
             "accepts it and `declines_offer` true when it declines it; neither when it does something else.")


def open_offer_for(meta: Optional[Mapping[str, Any]], records_source: Any, user_turn: bool) -> Optional[Dict[str, Any]]:
    """The offer the previous reply of this conversation left open on the one open quest, or None."""
    quest_id = single_quest_id(meta)
    if not (user_turn and quest_id and records_source is not None and getattr(records_source, "open_offer", None)):
        return None
    try:
        return records_source.open_offer(quest_id, str((meta or {}).get("conversation_id") or ""))
    except Exception as error:  # noqa: BLE001
        log.warning("open offer not read: %s: %s", type(error).__name__, str(error)[:200])
        return None


def open_offer_note(offer: Mapping[str, Any]) -> str:
    return OPEN_NOTE.format(pieces=", ".join(OFFER_WORDS.get(p, p) for p in (offer.get("pieces") or [])))


def handle_offer(plan: Any, rs: Any, records_source: Any) -> str:
    """When the planner reported that the message accepts or declines the open offer, code keeps it or remembers the decline, and the reply directive says what happened."""
    offer = getattr(rs, "open_offer", None)
    if not offer or records_source is None or getattr(plan, "action", None) != "answer":
        return ""
    if getattr(rs, "offer_receipt", ""):
        return "--- THE OFFER WAS CARRIED OUT ---\nResult: " + rs.offer_receipt + "\nSay only what this result says."      # a second pass over the same turn never carries it out twice
    if getattr(plan, "accepts_offer", False) and getattr(plan, "declines_offer", False):
        return ""      # contradictory structured fields: nothing is kept and nothing is declined (a decline that kept records, or an accept that dropped the offer, is the worse error)
    try:
        if getattr(plan, "accepts_offer", False):
            result = records_source.accept_offer(str(offer["id"]))
            log.info("offer accepted: status=%s steps=%s", result.get("status"), [(s.get("table"), s.get("status")) for s in result.get("steps") or []])
            rs.offer_receipt = str(result.get("message") or "Not changed: the offer could not be carried out just now.")      # the reply is this sentence, composed by code
            return "--- THE OFFER WAS ACCEPTED, AND CODE HAS CARRIED IT OUT ---\nResult: " + rs.offer_receipt + "\nSay only what this result says."
        if getattr(plan, "declines_offer", False):
            records_source.decline_offer(str(offer["id"]))
            rs.offer_receipt = "Understood, I will not offer to keep this again."
            return "--- THE OFFER WAS DECLINED ---\nAcknowledge it in one short sentence and do not offer to keep this again."
    except Exception as error:  # noqa: BLE001 -- said plainly, never claimed as done
        log.warning("open offer not carried out: %s: %s", type(error).__name__, str(error)[:200])
        rs.offer_receipt = "Not changed: the offer could not be kept just now."
        return "--- THE OFFER COULD NOT BE CARRIED OUT ---\nSay in one sentence that it could not be kept just now and nothing was changed."
    return ""


def settle_offer(rs: Any, res: Any) -> bool:
    """The reply to an accepted or declined offer is the one sentence code composed from what it did (the keep result, or the decline), never the model's text: a model told to
    describe a write it did not make is how a reply claims more than was done. True when the reply was replaced."""
    receipt = getattr(rs, "offer_receipt", "")
    if not receipt or getattr(res, "kind", "") != "answer":
        return False
    res.text = receipt
    return True
