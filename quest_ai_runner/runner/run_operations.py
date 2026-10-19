"""Which Quest operations a kind of run is pinned to.

Relevance (BM25 over the tool catalog) decides most of what a run's brief shows. This module is the
exception, and it is deliberate: some kinds of run owe an operation on EVERY pass, however its brief
happens to be worded, so a lexical match against a long brief cannot be trusted to surface it. The
pin travels as ``context_meta["tool_pins"]``; the orchestrator renders pinned tools first in the
deep brief and the planner's tool block (``ToolRegistry.select``). A run that is not in a listed
kind pins nothing and sees only what its words call for.

The how-to for each pinned operation is its registry entry (``core/tools_quest_ops.py``), never text
in a prompt, so the standing instructions can say WHAT to do ("record it with update_ask") and the
entry says HOW.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from ..core.tools_quest_intents import SPEC_KEEPING_PINS
from ..core.tools_quest_ops import AUTOPILOT_PINS
from ..core.tools_quest_work import AUTOPILOT_WORK_PINS
from .executor_support import _autopilot_composed_text


def pinned_operations(task: Dict[str, Any]) -> Tuple[str, ...]:
    """The operation names this task's run is pinned to (empty for most runs)."""
    if _autopilot_composed_text(task):
        return AUTOPILOT_PINS + AUTOPILOT_WORK_PINS + SPEC_KEEPING_PINS
    return ()
