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
