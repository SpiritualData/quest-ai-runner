"""An order whose work belongs to an attached environment is handed to it, never answered as a question (a step of the reach check).

Role: the reach judge's structured verdict says where what a request needs lives (``reach``), which attached environment covers it (``covered_by``) and whether the request
asks for something to be done (``asks_change``). When all three say "outside, covered, an order", the planner's own reading of the message as a question is overruled for the
turn (the turn is an order, so no information budget applies), and a plan that only answers is sent back once with the instruction to hand the work over: ``deferred_deep``
carrying the work, and the owned next step recorded on the quest the work belongs to. Fields of the judge's verdict only; no word of any message or reply is read.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

HANDOFF_NOTE = (
    "HAND-OFF REQUIRED: the reach check settled that this is an order, that its work lives outside what you can read, and that an attached environment covers it. Do not answer it "
    "as a question and do not say there is nothing to hand over. Plan the hand-off now: set deferred_deep to the work in full (what to build, check and fix, and what done looks like), "
    "and record the owned next step on the quest the work belongs to (create_or_update_quest_goal, owner and date from the records) or create the task for the covering "
    "environment (create_task). Then the reply says what was handed to whom, what already exists in the records and what is still missing, and does not end by asking what to do.")


def must_hand_off(verdict: Optional[Mapping[str, Any]]) -> bool:
    """True when the reach verdict says: outside, an environment covers it, and it asks for something to be done."""
    return bool(verdict and verdict.get("reach") == "outside" and verdict.get("covered_by") and verdict.get("asks_change") is True)


# --- The hand-off steps: what a hand-off must hold before the work is accepted as handed over -------------------------------------------------------------------------------
# A plan that hands the work over (a deep action or a ``deferred_deep``) is accepted only once the turn (1) read the quest's records that say what exists and what is missing
# (its goals, notes and specification records) and (2) made one owned next-step write. Both are facts about which tool receipts the turn holds, never about words.

RECORD_READS = ("list_quest_goals", "list_quest_notes", "read_spec_records")
STEPS_NOTE = (
    "HAND-OFF NOT YET COMPLETE: before the work is handed over, {missing}. Do that now (one step), then hand the work over; the reply says what was handed to whom, what already "
    "exists in the records and what is still missing.")
READ_STEP = "read what the quest's records say exists and what is missing (its goals, notes and specification records)"
WRITE_STEP = "record exactly one owned next step: the keyed next-step goal on the quest (create_or_update_quest_goal) or the task for the covering environment (create_task)"


def hands_off(plan: Any) -> bool:
    """True when the plan hands the work over: a deep action, or an answer that carries ``deferred_deep``."""
    return bool(plan is not None and (getattr(plan, "action", None) == "deep" or getattr(plan, "deferred_deep", None)))


def records_read(gathered: Any) -> bool:
    """True when a planner step read the quest's records (a receipt of a record-reading tool; a read the code made at intake does not count)."""
    return any(isinstance(g, Mapping) and str(g.get("locator") or "").removeprefix("tool:") in RECORD_READS and str(g.get("locator") or "").startswith("tool:") and not g.get("intake")
               for g in gathered or [])


def steps_missing(gathered: Any, registry: Any) -> list:
    """``["read", "write"]`` minus what the turn already holds, in that order."""
    from quest_ai_runner.the_world.tools import tool_guards
    missing = [] if records_read(gathered) else ["read"]
    if not tool_guards.mutating_tool_ran(gathered or [], registry):
        missing.append("write")
    return missing


def steps_note(missing: list) -> str:
    return STEPS_NOTE.format(missing=" and ".join({"read": READ_STEP, "write": WRITE_STEP}[m] for m in missing))


def owed_read_calls(quest_id: str, gathered: Any) -> list:
    """The record-reading tool calls for ``quest_id`` that no receipt of the turn holds yet (tool calls in the planner's own shape)."""
    done = {str(g.get("locator") or "") for g in gathered or [] if isinstance(g, Mapping) and not g.get("intake")}
    return [{"name": name, "args": {"quest_id": quest_id}} for name in RECORD_READS if f"tool:{name}" not in done] if quest_id else []
