"""The metric record of ONE chat turn that changes something: the same begin and finish as a deep goal (``goal_metrics``).

``critical-thinking:every_request_measured``. A chat turn or in-process work that plans a change is held to the metric that serves it by
the same code as a deep goal: the slice of the specification is found, the cheap recorded-row meter reads the metric before the work
(``begin``), and the meters read it again when the reply is built (``finish``, bounded by the same arm timeout), and both readings are
rows of the run's ledger. No model call, and nothing here reads words of a reply (hard rule #3). A turn with no slice, or whose meters
answer nothing, keeps no rows and waits for nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from esoi import payload_of, render_line

from quest_ai_runner.the_intelligence.critical_thinking.metric_translation.goal_metrics import GoalMetrics


class TurnMetrics:
    """One turn's record, kept by a ``GoalMetrics`` that is never gated (a chat turn has no worker to ask for a result)."""

    def __init__(self, tools: Any, iteration: Any, run_id: str, *, request: str = "", working_dir: Optional[str] = None,
                 status: Optional[Callable[[str], None]] = None) -> None:
        self.goal = GoalMetrics.for_goal(None, iteration, run_id, request=request, working_dir=working_dir, status=status)
        self.goal.gate_applies = False

    def begin(self) -> Optional[Dict[str, Any]]:
        """Read the metric before the work. The row of that reading (``name``, ``before``) or None when nothing was read. Never raises."""
        try:
            self.goal.begin()
            rows = payload_of(self.goal.rows())
        except Exception:  # noqa: BLE001
            return None
        return next((dict(r) for r in rows if r.get("before") is not None), None)

    def finish(self, res: Any, *, changed: bool) -> List[Dict[str, Any]]:
        """Read it again, reconcile, and put the rows on the turn's result (``metric_results``). The rows, empty when none were kept."""
        try:
            done = SimpleNamespace(output="", decision_id=None, has_write_receipt=changed, changes_landed=0)
            self.goal.note_attempt(done, [])
            self.goal.finish(done)
            res.metric_results = list(getattr(done, "metric_results", None) or [])
            return list(res.metric_results)
        except Exception:  # noqa: BLE001
            return []


def line_of(rows: List[Dict[str, Any]]) -> str:
    """The one plain line the rows make (``Metric: name before -> after (source)``), written by code."""
    return render_line(rows)
