"""The metric record of ONE deep goal, kept by the flow: before the work, around it, and what every consumer is handed.

ESOI's rule for work is "what the relevant metric result was before and after your change". This module is how a deep goal
meets it without trusting anyone's closing words. For each goal the goal loop creates one ``GoalMetrics`` and calls it at four
points, all in code, none of them a model call:

  ``begin``         before the first attempt: the arm's slice of the specification becomes a short NEXT PRIORITY block for
                    the brief, and an arm that measures reads the metric and writes the ``measured`` ``before`` row. That
                    reading is bounded and announced (``metric_meter.read_value``): it never holds the worker back for
                    longer than its budget, and with no slice (no records, no folder) nothing is read and nothing waits.
  ``note_attempt``  after each attempt: whether the run CHANGED something, from the runner's own records (a tool that acts
                    succeeded in the tool-call ledger, the runner's write receipts, or the changes it counted as landed),
                    never from the worker's words.
  ``wants_nudge``   before the attempt is verified: the gate. A run that changed something and holds no row at all (no
                    measured row, no result the worker recorded, no declared not-applicable) is asked once, in the worker's
                    own session, to record it. That one request is an attempt of the goal's existing budget; after it the
                    flow accepts what there is and marks ``metric_missing``. It never loops, and it needs a runner that can
                    resume the session (a re-run would repeat the work), so a runner that cannot is not gated.
  ``finish``        after the last attempt: the arm reads the metric again (``after``, by the meters that read the tree as
                    it is now, never a recorded row), the ledger's rows are reconciled
                    (``metric_results.reconcile``), and the result carries them: ``DeepResult.metric_results`` and
                    ``metric_missing``, plus one plain line appended to its text for consumers that show text only.

The gate applies only when the worker was given ``record_metric_result``. It is judged on the ledger and the runner's own
records and never on what the worker or the verifier wrote (hard rule #3).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .metric_results import (MAX_METRICS, SOURCE_MEASURED, MetricLedger, MetricResult, metric_ledger_from_env,
                             payload_of, reconcile, render_line)
from .runtime_settings import process_environment
from .spec_slice import SpecSlice, next_priority_block
from .tools_failure_ledger import ToolOutcome, changed_something
from .tools_metric_results import TOOL_NAME

# The one request made of a worker whose change has no metric result on record. Written here, not by a model. It yields to
# the person's own request: code never reads the request for that (hard rule #3), the worker, who has it, decides, and a
# worker that records nothing leaves the ledger empty, which ends as ``metric_missing``, the honest outcome.
NUDGE_BRIEF = (
    "Your change has no metric result on record. If the request you were given told you not to record a metric, record "
    "nothing: say so in one sentence and stop. Otherwise record it now with record_metric_result: the value of the "
    "metric this work serves before and after your change (a stage its cascading conditionals did not call for stays "
    "empty, and the reason says so), or not_applicable with a one-sentence reason if no metric "
    "applies. Do not redo or extend the work, and do not repeat your earlier summary: reply with one sentence saying "
    "what you recorded.")
# The two statuses below are fixed sentences a person reads on an 80-column screen beside the clock and the spinner, so
# they stay near 50 characters (the terminal cuts a longer one with an ellipsis).
NUDGE_STATUS = "Asking the worker to record the metric result."
# A run that did none of the work leaves nothing to measure or to find missing.
UNFINISHED_FLAGS = ("launch_failed", "cancelled", "usage_limited", "paused", "deferred")
# The moment a reading is taken, as the ledger's reason names it.
READING_PHASES = {"before": "before the change", "after": "after the change"}
# What the status line says while a reading runs, and what it says when none could be taken in time. Plain words for a
# person: never the metric's name (a long one was cut mid-word on a narrow screen).
READING_STATUS = {
    "before": ("Checking the starting value before any change.", "No starting value in time. Going on without it."),
    "after": ("Checking the result after the change.", "No result check in time. Going on without it.")}


def working_dir_of(runner: Any, override: Optional[str]) -> Optional[str]:
    """The folder a goal's worker starts in: the per-run override, else the runner's configured one, else unknown."""
    return override or getattr(getattr(runner, "cfg", None), "working_dir", None) or None


def run_hint(tools: Any, run_id: str) -> str:
    """The sentence for the deep brief's tool section that tells the worker which run its results are kept under."""
    if tools is None or tools.get(TOOL_NAME) is None:
        return ""
    return (f"\nThis run's id is {run_id}: add --run {run_id} to a {TOOL_NAME} call so the result is kept "
            f"for this run.")


def deep_metric_data(result: Any) -> Dict[str, Any]:
    """The metric payload of a finished turn, from its deep results alone: ``{"metrics": [...]}`` (capped, in goal order)
    and ``"metric_missing": True`` when any goal changed something and ended with no metric result recorded."""
    results = list(getattr(result, "deep_results", None) or [])
    data: Dict[str, Any] = {"metrics": [dict(e) for d in results for e in (getattr(d, "metric_results", None) or [])][:MAX_METRICS]}
    if any(getattr(d, "metric_missing", False) for d in results):
        data["metric_missing"] = True
    return data


class GoalMetrics:
    """The metric record of one deep goal. Created per goal; never raises into the goal loop."""

    def __init__(self, iteration: Any, ledger: MetricLedger, run_id: str, *, task_id: Optional[str] = None,
                 request: str = "", working_dir: Optional[str] = None, gate_applies: bool = False,
                 status: Optional[Callable[[str], None]] = None):
        self.iteration, self.ledger, self.run_id = iteration, ledger, run_id
        self.task_id = task_id or ""
        self.request, self.working_dir = request, working_dir
        self.gate_applies = gate_applies
        self.status = status
        self.started = time.time()
        self.spec_slice: Optional[SpecSlice] = None
        self.attempts = 0
        self.changed = False
        self.nudged = False
        self.carried: Any = None
        self.missing = False
        self.gaps: List[str] = []   # readings that could not be taken, said on the rows written after them

    @classmethod
    def for_goal(cls, tools: Any, iteration: Any, run_id: str, **kwargs: Any) -> "GoalMetrics":
        """The goal's record, on the registry's ledger (else the environment's) and gated only if the tool was offered."""
        ledger = getattr(tools, "metric_ledger", None) or metric_ledger_from_env(process_environment())
        offered = tools is not None and tools.get(TOOL_NAME) is not None
        return cls(iteration, ledger, run_id, gate_applies=offered, **kwargs)

    def say(self, text: str) -> None:
        if self.status is not None:
            self.status(text)

    # -- before the first attempt ------------------------------------------------------------------

    def begin(self) -> str:
        """The block for the brief ("" for none), after reading the metric when the arm measures. Never raises."""
        try:
            self.spec_slice = self.iteration.slice_for(self.request, self.working_dir)
            found = self.spec_slice
            if found is None:
                return ""
            if self.iteration.measures and found.metric_id:
                self.read("before")
            return "\n\n" + next_priority_block(found)
        except Exception:  # noqa: BLE001  the record must never stop the work it records
            return ""

    def read(self, when: str) -> None:
        """Take one reading of the slice's metric, ``before`` or ``after`` the change, and keep it as a measured row.

        The reading is bounded, and announced only if a meter that takes time is about to run. One that gives nothing
        writes no row (a row without a number would count as a record); when it ran out of time that is said on the status
        line and, for a reading before the change, on the row written after the work (the note names the meter; the
        status line does not)."""
        found = self.spec_slice
        if found is None:
            return
        phase = READING_PHASES[when]
        checking, missed = READING_STATUS[when]
        reading = self.iteration.read_metric(found, self.working_dir, when=when, announce=lambda: self.say(checking))
        if reading.value is not None:
            self.keep(reading.meter, **{when: reading.value})
        elif reading.note:
            self.gaps.append(f"no reading {phase}: {reading.note}")
            self.say(missed)

    def keep(self, meter: str, before: Optional[float] = None, after: Optional[float] = None) -> None:
        found = self.spec_slice
        if found is None:
            return
        reason = f"measured by {meter}" + (f"; {'; '.join(self.gaps)}" if self.gaps and after is not None else "")
        self.ledger.record(MetricResult(
            source=SOURCE_MEASURED, run_id=self.run_id, task_id=self.task_id, solution_id=found.solution_id,
            metric_id=found.metric_id, name=found.metric_name, milestone=found.milestone, before=before,
            after=after, reason=reason))

    # -- around the attempts -----------------------------------------------------------------------

    def note_attempt(self, res: Any, window: Sequence[ToolOutcome]) -> None:
        """Count the attempt, and remember that the run changed something when its own records say so."""
        self.attempts += 1
        if changed_something(window) or getattr(res, "has_write_receipt", False) or getattr(res, "changes_landed", 0):
            self.changed = True

    def rows(self) -> List[MetricResult]:
        """Every row kept for this goal: under its run id, and what a call with only ``--task`` kept since it began."""
        rows = list(self.ledger.for_run(self.run_id))
        if self.task_id and self.task_id != self.run_id:
            rows += [r for r in self.ledger.for_run(self.task_id) if r.ts >= self.started]
        return rows

    def wants_nudge(self, attempt: int, max_attempts: int, resumable: bool) -> bool:
        """The gate: a changed run with no row at all, once, with an attempt left and a session to resume."""
        return bool(self.gate_applies and self.changed and not self.nudged and resumable
                    and attempt < max_attempts and not self.rows())

    def nudge(self, res: Any) -> str:
        """Spend the one request: remember the attempt that is being asked, and return the brief to resume it with."""
        self.nudged, self.carried = True, res
        self.say(NUDGE_STATUS)
        return NUDGE_BRIEF

    def merge(self, res: Any) -> Any:
        """The result of the attempt after a nudge, joined to the attempt it continued.

        The resumed session's last message answers the request; the work that was done is the earlier attempt's, so its
        text, receipts and future context are kept in front. If the resumed attempt produced nothing, the earlier attempt
        stands as it was."""
        earlier, self.carried = self.carried, None
        if earlier is None:
            return res
        if not (getattr(res, "output", "") or "").strip():
            return earlier
        res.output = (earlier.output or "").rstrip() + "\n\n" + res.output.strip()
        res.observations = list(earlier.observations or []) + list(res.observations or [])
        res.has_write_receipt = bool(res.has_write_receipt or earlier.has_write_receipt)
        res.changes_landed = (res.changes_landed or 0) + (earlier.changes_landed or 0)
        res.observations_reported = bool(res.observations_reported or earlier.observations_reported)
        res.future_context = res.future_context or earlier.future_context
        return res

    # -- after the last attempt --------------------------------------------------------------------

    def finish(self, res: Any) -> None:
        """Read the metric once more, reconcile the rows, and put them on the result. Never raises."""
        try:
            did_work = (self.attempts > 0 and not getattr(res, "decision_id", None)
                        and not any(getattr(res, flag, False) for flag in UNFINISHED_FLAGS))
            found = self.spec_slice
            if did_work and found is not None and self.iteration.measures and found.metric_id:
                self.read("after")
            results = reconcile(self.rows())
            self.missing = bool(did_work and self.gate_applies and self.changed and not results)
            res.metric_results = payload_of(results)
            res.metric_missing = self.missing
            line = render_line(res.metric_results, self.missing) if did_work else ""
            if line and (res.output or "").strip():
                res.output = res.output.rstrip() + "\n\n" + line
        except Exception:  # noqa: BLE001
            pass

    def event_data(self, res: Any) -> Dict[str, Any]:
        """The structured part of an event that reports this goal finished: ``metrics``, and ``metric_missing`` when set."""
        data: Dict[str, Any] = {"metrics": list(getattr(res, "metric_results", None) or [])}
        if getattr(res, "metric_missing", False):
            data["metric_missing"] = True
        return data
