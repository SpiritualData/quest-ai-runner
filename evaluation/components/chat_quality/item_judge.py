"""Per-item verdicts for the chat_quality judge, each decided by a majority of independent fast-tier views (the hub's Decision Check, ``majority_vote`` arm).

The whole-case judge returns a pass or fail for every rubric item in one sample, and the same evidence judged again moved items near the pass line (13 of 50 saved
case verdicts flipped across three fresh judgments, 2026-11-08). Here each item is a pick between ``pass`` and ``fail``: the whole-case judge's own verdict on the
item is the draft vote, three fast-tier views each see the item and the turn's evidence and must quote the words that decide it (code checks the quote is in that
evidence, or the view does not count), and the pick more than half of the opinions hold is the item's verdict; only a tie goes to the hub's adjudication call.
The case score is then the judge's existing formula over the item verdicts, with its weights unchanged. Off by default; ``QUAL_ITEM_VIEWS=1`` turns it on.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

VIEWS = 3
EVIDENCE_CHARS = 14000
OPTIONS = [{"id": "pass", "text": "the turn meets this item"}, {"id": "fail", "text": "the turn does not meet this item"}]
INSTRUCTION = ("Does the turn meet this rubric item? {item}\n"
               "Judge only this item, from the evidence: the person's message, the reply exactly as sent, what the turn read, the changes it attempted and the harness checks. "
               "In this replay every write is blocked and recorded: a write listed in the changes counts as made for an item about what exists after the run. "
               "Pick pass only if the evidence shows the item is met, and quote the words that show it. Pick fail when it is not met or the evidence does not show it, "
               "and quote the words that show that when there are any.")


def on() -> bool:
    """Off until it meets its target on saved inputs (at most 2 of 50 verdicts flipping; 6 of 50 on 2026-11-08, the views not yet shown the state check). QUAL_ITEM_VIEWS=1 turns it on."""
    return os.environ.get("QUAL_ITEM_VIEWS", "0") == "1"


def harness_text(pre: Any, state: Any) -> str:
    """The harness's own checks, in words a view can quote: each precheck with its result, and the state check when the run made one."""
    lines = [f"- precheck {c.get('name')}: {'ok' if c.get('ok') else 'FAILED'} ({c.get('detail')})" for c in ((pre or {}).get("checks") or []) if isinstance(c, dict)]
    if isinstance(state, dict):
        lines.append(f"- state check: {'ok' if state.get('ok') else 'FAILED'} ({state.get('why')})")
    return "\n".join(lines) or "(none)"


def evidence_text(case: Dict[str, Any], evidence: Dict[str, Any], changes: List[str], truth: str, pre: Any = None, state: Any = None) -> str:
    def clip(text: Any, limit: int) -> str:
        text = str(text or "")
        return text if len(text) <= limit else text[:limit] + " ...[cut]"
    return ("The person's message:\n" + clip(case.get("input") or case.get("input_text"), 2000)
            + "\n\nThe reply exactly as sent:\n" + clip(evidence.get("reply"), 4000)
            + "\n\nWhat the turn read:\n" + clip(truth, EVIDENCE_CHARS)
            + "\n\nThe changes the turn attempted:\n" + (clip("\n".join(changes or []), 2000) or "(none)")
            + "\n\nThe harness checks:\n" + harness_text(pre, state)
            + "\n\nNotes on the setting:\n" + clip(case.get("evidence_notes"), 3000))


def refine(case: Dict[str, Any], verdict: Dict[str, Any], evidence: Dict[str, Any], changes: List[str], truth: str, ask, pre: Any = None, state: Any = None) -> Dict[str, Any]:
    """``verdict`` with each rubric item's pass decided by the majority of its draft and its views, the item's views kept, and the score recomputed by the judge's formula."""
    from hubofhubs.api import CheckBinding, MajorityVote, View
    shown = evidence_text(case, evidence, changes, truth, pre, state)

    def decide(item: Dict[str, Any]) -> Dict[str, Any]:
        views = [View(f"view{n + 1}", INSTRUCTION.format(item=item["item"]), shown) for n in range(VIEWS)]
        binding = CheckBinding(kind="pick", options=OPTIONS, views=views, max_calls=VIEWS + 1, parallel=VIEWS,
                               draft_basis=str(item.get("evidence") or ""))
        got = MajorityVote(plumbing=None).check("pass" if item["pass"] else "fail", binding, ask)
        pick = got.get("pick") or ("pass" if item["pass"] else "fail")
        return dict(item, majority={"outcome": got.get("outcome"), "agreement": got.get("agreement"), "calls": got.get("calls"),
                                    "draft": "pass" if item["pass"] else "fail"}, **{"pass": pick == "pass"})
    with ThreadPoolExecutor(3) as pool:
        items = list(pool.map(decide, verdict.get("rubric") or []))
    refined = dict(verdict, rubric=items)
    import judge as J
    refined["score"] = J.formula_score(case, refined)
    return refined
