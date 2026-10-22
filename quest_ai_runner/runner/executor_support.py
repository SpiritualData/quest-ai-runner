"""Executor helpers: autopilot detection, session ids, the progress sink and outcome type.

Split out of ``executor.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass

from ..core.orchestrator import OrchestratorResult
from ..core.adapters import ProgressEvent
from ..core.goal_metrics import deep_metric_data
from ..core.metric_results import MAX_METRICS
from .executor_contracts import log


# Same throttle, same reasoning, for the ``pending_inputs`` callable built by
# ``_build_pending_inputs``: a human typing a mid-task steering message is rare and not
# latency-sensitive down to the second, so claiming on every internal loop boundary would hammer
# the Quest API for no benefit. A real message is never lost by the throttle, only delayed by up
# to this many seconds until the next allowed claim.
MESSAGE_CLAIM_INTERVAL_SECONDS = 15.0


# While a deep worker holds a live two-way session, a steering message is only useful if it is
# quick: the worker looks for new messages, and for a stop or pause request, this often. The cost
# is one cheap call per active run per interval. Used by the ``RunControl`` the executor builds.
RUN_CONTROL_POLL_SECONDS = 2.0


# Hard cap on the FALLBACK prior-conversation read in ``_build_context_view`` (the path taken only
# when no ConversationStore is wired). Without a cap, a long-running conversation would dump its
# entire transcript into every linked task's prompt, growing without bound as the conversation
# grows. Passed as ``max_bytes`` to ``read_section`` so the serving adapter truncates at the
# source; adapters that serve conversations keep the recent tail when truncating (see
# ``ClaudeConversationsAdapter.read_section``).
CONV_CONTEXT_MAX_BYTES = 16_000


# The value that marks a task as the recurring AUTOPILOT PASS (see ``runner.autopilot``).
AUTOPILOT_PASS_KIND = "autopilot"


def _is_autopilot_pass(task: Dict[str, Any]) -> bool:
    """Whether this task is the recurring autopilot pass (routed to ``AutopilotPass``).

    Reads ``task_kind`` FIRST, and that ordering is the whole point. ``task_kind`` is a PERSISTENT
    classification: the Quest API writes it once at create and never touches it again, and a
    recurring series' spawned occurrences inherit it. ``handler``, by contrast, is the CLAIMING
    WORKER'S OWN LABEL -- the poller stamps it on every ``claim()`` with the rep slug or runner
    label, OVERWRITING whatever was there. So routing on ``handler`` alone is unsound: it survives
    only because each recurring occurrence happens to be a fresh document, and it breaks the moment
    a task is re-polled, retried, or resumed after a claim (the claim label has replaced
    "autopilot", and the pass task would then be run as an ordinary deep task).

    ``handler`` is still accepted as a BACK-COMPAT fallback so a pass task queued before the
    backend gained ``task_kind`` still routes correctly.
    """
    if str(task.get("task_kind") or "").strip().lower() == AUTOPILOT_PASS_KIND:
        return True
    return str(task.get("handler") or "").strip().lower() == AUTOPILOT_PASS_KIND


# The value the autopilot pass stamps on every batch/goal-proposal task it creates (see
# ``runner.autopilot``). Duplicated locally for the same reason as ``AUTOPILOT_PASS_KIND`` above:
# this module must not import ``runner.autopilot``, which is a consumer of this executor, not a
# dependency of it.
AUTOPILOT_WORK_KIND = "autopilot_work"


def _autopilot_composed_text(task: Dict[str, Any]) -> bool:
    """Whether this task's own text was composed by the autopilot pass (see ``compose_batch_text``).

    This is the routing-metadata signal that replaces scanning the task's text for
    ``BLOCK_START``. A text-substring test is unsound: the autopilot pass's context-updates block
    is plain text, so it survives verbatim when someone pastes a prior run's brief into chat, and
    the backend files that paste as its OWN, brand-new task. That task's ``task_kind`` is unset (or
    something other than ``AUTOPILOT_WORK_KIND``), so it is correctly read here as ordinary text
    that merely quotes a block, not as a batch this executor can trust to carry a live one.

    ``task_kind`` is written once at task creation and never overwritten by the claim/status/
    progress paths, so it reliably reflects what actually composed the text, unlike scanning that
    text for a marker string which anyone's task can happen to contain.
    """
    return str(task.get("task_kind") or "").strip().lower() == AUTOPILOT_WORK_KIND


def task_scope_tags(task: Dict[str, Any]) -> List[str]:
    """The quest scope tags (``core.scope_tags``) a task's run carries, from its quest and goal ids.

    A task created against a quest holds the quest's id in ``goal_id`` and leaves ``quest_id``
    empty (see ``_build_context_view``), so a scope derived from ``quest_id`` alone left every such
    run unscoped, and every fenced context arm (past turns, recent context, vector hits) served it
    material from any quest. Both ids are tagged: whichever names the quest, the run is fenced to it.
    """
    from ..core.recent_context import quest_scope_key
    ids = [task.get("quest_id"), task.get("goal_id")]
    return list(dict.fromkeys(quest_scope_key(str(i)) for i in ids if i))


def across_pass_resume(task: Dict[str, Any], resume_session_id: Optional[str]) -> Optional[str]:
    """The session this task's run should resume, or None for a fresh start.

    AN AUTOPILOT WORK THREAD STARTS EACH PASS FRESH. Its brief is composed anew every pass and
    already carries the compact carry-over a resume was meant to provide: what the last run
    produced, the thread's recent run results, and what changed since. Resuming the previous
    pass's session on top replays every earlier pass's whole transcript, which grows without bound:
    on 2026-10-06 two threads resumed 5 MB transcripts holding every earlier pass's prompt, and the
    model refused the run ("Prompt is too long").

    The exception is a pass that was PAUSED on the Claude usage limit (``limit_hit_count`` > 0,
    which the backend counts only while one waiting episode lasts): that is the same pass picking up
    where it stopped, and its session is exactly what it needs. Every other task keeps its resume,
    still subject to the deep runner's size check (``SubprocessGoalRunner.run_goal_once``).
    """
    if not resume_session_id or not _autopilot_composed_text(task):
        return resume_session_id
    if int(task.get("limit_hit_count") or 0) > 0:
        return resume_session_id
    log.info("task %s is an autopilot work thread: starting this pass fresh instead of resuming "
             "session %s (the brief carries the carry-over)", task.get("task_id") or task.get("id"),
             resume_session_id)
    return None


def terminal_session_id(result: OrchestratorResult) -> Optional[str]:
    """The session id THIS run leaves behind for the next run on the same thread, or None.

    A turn can produce several deep results (one per fanned-out subgoal), and only one id can be
    the thread's. We take the LAST one that carries a session, because that is the session holding
    the most recent state: the subgoals run in order, so the last worker to open a session is the
    one whose transcript ends nearest to where a follow-up would pick up. It is a choice, not a
    fact about which session is "best" -- a fan-out genuinely has several live sessions and the
    thread can only continue one of them. (Within-run continuation is unaffected: a continued
    attempt keeps its OWN session id, so the id reported here is still that subgoal's one thread.)

    None when nothing produced a session: a plain answer turn, a crash before the worker launched,
    or a deep runner that is not a subprocess. Reporting nothing is deliberate, since an empty
    string would claim a session exists and make the next run try to resume it.
    """
    for deep in reversed(list(getattr(result, "deep_results", None) or [])):
        sid = (getattr(deep, "session_id", None) or "").strip()
        if sid:
            return sid
    return None


def _run_accepts(fn: Any, name: str) -> bool:
    """Whether the callable ``fn`` takes a parameter called ``name`` (False when it cannot be told)."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def utc_stamp(moment) -> str:
    """An aware datetime as "YYYY-MM-DDTHH:MM:SSZ" (the backend's start_at form)."""
    from datetime import timezone as _tz
    return moment.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_facts(metrics: Any, missing: Any) -> Dict[str, Any]:
    """The metric facts a report or a progress post carries: ``{"metrics": [...], "metric_missing": True}``.

    Only the keys that have something to say are present, so a run with neither adds nothing to what is sent. The
    entries are the runner's own (``MetricResult.to_dict()``, shape in ``docs/metric-results.md``), copied and capped
    at the payload's size, never edited; anything that is not a list of dicts, and any ``missing`` other than the
    boolean ``True``, says nothing."""
    listed = metrics if isinstance(metrics, (list, tuple)) else []
    entries = [dict(entry) for entry in listed if isinstance(entry, dict)][:MAX_METRICS]
    facts: Dict[str, Any] = {}
    if entries:
        facts["metrics"] = entries
    if missing is True:
        facts["metric_missing"] = True
    return facts


def metric_report_data(result: Any) -> Dict[str, Any]:
    """The metric facts of a finished turn, for its terminal report (``result_data``); ``{}`` when it has none.

    Taken from what the run produced (``OrchestratorResult.metric_results`` / ``.metric_missing``, which the goal loop
    fills from its metric ledger), never worked out again here. A result that carries them on its deep results only,
    such as one an integrator builds for ``report()``, is read the way the loop reads it (``deep_metric_data``)."""
    facts = metric_facts(getattr(result, "metric_results", None), getattr(result, "metric_missing", False))
    if facts:
        return facts
    fallback = deep_metric_data(result)
    return metric_facts(fallback.get("metrics"), fallback.get("metric_missing"))


def client_accepts_named_param(fn: Any, name: str) -> bool:
    """Whether a client method declares a parameter called ``name`` EXPLICITLY. Stricter than
    ``client_accepts_session_id``: a ``**kwargs`` catch-all (a test double, a mock) does not count,
    so an older client keeps being called with exactly the arguments it always was."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def client_accepts_session_id(fn: Any) -> bool:
    """Whether a client's report method takes a ``session_id`` keyword (or ``**kwargs``).

    Same opt-in discipline the orchestrator applies to a deep runner's ``run_goal`` kwargs: an
    older client (or a consumer's own, or a test double) that has never heard of session continuity
    keeps being called with exactly the arguments it has always been called with. Unresolvable
    signatures answer False, so the safe, unchanged call is the default.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins, C callables, exotic mocks
        return False
    for p in params.values():
        if p.name == "session_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str                       # "done" | "needs_you" | "incomplete" | "failed" | "cancelled" | "waiting" | "paused"
    result: str = ""
    decision_id: Optional[str] = None


def _model_used_note(deep: List[Any]) -> str:
    """A trailing note naming which LLM model(s) executed this deep work, or "" when unknown.

    ``deep`` is a completed run's list of ``DeepResult``s (more than one on a fan-out); each may
    carry ``.model`` (the ladder rung the goal loop actually dispatched it to, see
    ``core/orchestrator.py``). Reports every DISTINCT model, not just the first, so a run that
    escalated mid-goal (or fanned out across subtasks starting at different rungs) is described
    honestly. Consumers (a chat UI, a task detail view) get this for free since it travels inside
    the same text every terminal report already carries -- no extra field or API surface needed.
    """
    models = set()
    for d in deep:
        tier = (getattr(d, "model", None) or "").strip()
        resolved = (getattr(d, "resolved_model", None) or "").strip()
        if tier and resolved and resolved != tier:
            models.add(f"{tier} ({resolved})")
        elif tier or resolved:
            models.add(tier or resolved)
    models = sorted(models)
    if not models:
        return ""
    label = "model" if len(models) == 1 else "models"
    return f"\n\n(Completed with {label}: {', '.join(models)}.)"


class _TaskProgressSink:
    """Routes orchestrator events to the task's live progress stream.

    Forwards all events EXCEPT raw streaming partials to report_progress, so the
    task-detail SSE stream shows step-by-step what the AI is doing (plan -> read ->
    answer) and live token counts. Milestones also go to ``on_milestone``, which keeps them on
    the task stream too: interim updates never post into the originating chat.
    """
    _SKIP = frozenset({"partial"})

    def __init__(self, task_id: str, report_fn: Callable, on_milestone: Optional[Callable]):
        self._task_id = task_id
        self._report = report_fn
        self._on_milestone = on_milestone

    def update(self, event: ProgressEvent, mode) -> None:
        if event.type in self._SKIP:
            return
        try:
            self._report(self._task_id, event.type, text=event.text, data=event.data)
        except Exception:  # noqa: BLE001
            pass
        try:
            if event.type == "milestone" and self._on_milestone:
                self._on_milestone(event)
        except Exception:  # noqa: BLE001
            pass
