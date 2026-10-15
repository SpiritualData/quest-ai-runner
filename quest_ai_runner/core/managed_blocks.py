"""The marker-delimited context-updates block: delimiters, receipt rules and the span parser.

Pure string primitives with no dependencies, shared by the runner (which writes and reads the
block, ``runner.context_updates``) and the prompt budget (``core.prompt_budget``, which keeps
exactly one block per prompt). They live in ``core`` so the budget does not import the runner;
``runner.context_update_types`` and ``runner.context_update_receipts`` re-export them.
"""
from __future__ import annotations

from typing import List, Tuple

# The block delimiters. They are parsed back out of a composed task text (see ``parse_manifest``),
# which is what lets a consumer render the receipt from the task it just ran without threading a
# bundle object through the queue.
BLOCK_START = "=== CONTEXT UPDATES ==="

BLOCK_END = "=== END CONTEXT UPDATES ==="

# The opening words of the receipt gate written right after the end marker.
GATE_HEAD = "BEFORE YOU FINISH, account for the context updates"

# The receipt protocol, stated to the run that will produce the work. Kept apart from
# ``core/context_doctrine`` because it is not a doctrine about how to think: it is the contract for
# one specific block, and it is meaningless without the refs that block carries.
RECEIPT_RULES = (
    'Write what you ACTUALLY did with it ("cited in the method section", "answered in the doc", '
    '"contradicts the plan, flagged"), not what it was about. "not used" is a fine and common '
    'answer -- say it plainly rather than inventing a use, because the person reads these lines to '
    'find out whether what they wrote reached you, and a made-up one is worse than an honest no. '
    "This is your own account of your own run: nothing else writes it for you, and no second pass "
    "goes back and guesses."
)


def block_spans(text: str) -> List[Tuple[int, int]]:
    """``(start, stop)`` of every context-updates block in ``text``, receipt gate included.

    A block runs from ``BLOCK_START`` to ``BLOCK_END``, and the receipt gate written right after
    the end marker (``usage_receipt_gate``) belongs to it: a gate whose block is gone asks the run
    to account for refs it was never shown. The gate ends with ``RECEIPT_RULES``; a gate written
    in some older wording is left in place rather than guessed at.

    The prompt budget (``core.prompt_budget``) reads this to keep exactly one block per prompt, so
    the format and the code that finds it live in one module.
    """
    body = str(text or "")
    spans: List[Tuple[int, int]] = []
    pos = 0
    gate_head = GATE_HEAD
    while True:
        start = body.find(BLOCK_START, pos)
        if start < 0:
            break
        end = body.find(BLOCK_END, start + len(BLOCK_START))
        if end < 0:
            stop = len(body)
        else:
            stop = end + len(BLOCK_END)
            gate = body.find(gate_head, stop)
            if 0 <= gate <= stop + 4:
                rules = body.find(RECEIPT_RULES, gate)
                nxt = body.find(BLOCK_START, gate)
                if rules >= 0 and (nxt < 0 or rules < nxt):
                    stop = rules + len(RECEIPT_RULES)
        spans.append((start, stop))
        pos = stop
    return spans
