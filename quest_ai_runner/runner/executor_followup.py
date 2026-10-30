"""Automatic follow-up runs for a task whose deep run left the ask clearly unfinished.

A mixin of ``TaskExecutor`` (see ``executor.py``): methods only, state lives on the instance. The
policy itself (when to continue, what the next brief says) is the pure code in ``auto_followup.py``;
this is the loop that applies it around ``Orchestrator.run`` and writes the audit trail.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from hubofhubs.models import outage as provider_outage

from ..core import usage_limit
from ..core.orchestrator import OrchestratorResult
from ..core.orchestrator_constants import AUTO_FOLLOWUP_MAX_RUNS, AUTO_FOLLOWUP_TOKEN_BUDGET
from .auto_followup import FollowUpDecision, assess, followup_brief, result_tokens
from .executor_contracts import log
from .executor_support import terminal_session_id

# How much of the original ask names the follow-up's goal line.
_GOAL_ASK_CHARS = 300


class FollowUpMixin:
    """Starting another deep run for the remaining items of an unfinished task."""

    def _follow_up_limits(self) -> Tuple[int, Optional[int]]:
        """``(max follow-up runs, token budget)`` for this executor's brain, defaults when unset."""
        cfg = getattr(self._orch, "cfg", None)
        runs = getattr(cfg, "auto_followup_max_runs", AUTO_FOLLOWUP_MAX_RUNS)
        if isinstance(runs, bool) or not isinstance(runs, int):
            runs = AUTO_FOLLOWUP_MAX_RUNS
        budget = getattr(cfg, "auto_followup_token_budget", AUTO_FOLLOWUP_TOKEN_BUDGET)
        if isinstance(budget, bool) or not (budget is None or isinstance(budget, int)):
            budget = AUTO_FOLLOWUP_TOKEN_BUDGET
        return max(0, runs), budget

    _WORK_TYPE_LABELS = {"web_search": "web search", "quest_ops": "Quest operations",
                         "quest_docs_ops": "Quest docs operations"}

    @classmethod
    def _declared_route(cls, task: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
        """``(known, work type labels)`` from what the task itself declares. A task is "known" when
        the person chose an environment (``env_id``) or listed its work types (``work_types``, any
        mix of web_search, quest_ops, quest_docs_ops). The planner would only rediscover that."""
        task = task if isinstance(task, dict) else {}
        raw = task.get("work_types")
        types = [str(t) for t in raw if isinstance(t, str) and t] if isinstance(raw, (list, tuple)) else []
        labels = [cls._WORK_TYPE_LABELS.get(t, t.replace("_", " ")) for t in types]
        return bool(types or task.get("env_id")), labels

    def _direct_deep_extra(self, ask: str, supports_deep_goal: bool,
                           task: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """``{"deep_goal": ...}`` for a task's FIRST run when it should skip the planner, else None.
        That is when the brain is set to send queued tasks straight to a deep run
        (``task_direct_deep``), or when the task already declares its environment or work types
        (``_declared_route``): a choice the person made needs no planner call to rediscover. The
        orchestrator builds the plan from the goal. Needs a brain whose ``run`` accepts
        ``deep_goal``; an older brain keeps the planner path."""
        cfg = getattr(self._orch, "cfg", None)
        known, labels = self._declared_route(task)
        enabled = getattr(cfg, "task_direct_deep", False) is True
        if not supports_deep_goal or not (enabled or (known and getattr(cfg, "task_route_declared", True) is not False)):
            return None
        goal = " ".join((ask or "").split())[:_GOAL_ASK_CHARS]
        if not goal:
            return None
        prefix = "Complete this task"
        if labels:
            prefix += " (work: " + ", ".join(labels) + ")"
        return {"deep_goal": prefix + ": " + goal}

    def _open_decision_ids(self, quest_id: Optional[str]) -> FrozenSet[str]:
        """The quest's open decision ids, empty when they cannot be read (never raises)."""
        probe = getattr(self._orch, "quest_open_decision_ids", None)
        if not callable(probe):
            return frozenset()
        try:
            ids = probe(quest_id)
            return frozenset(ids) if isinstance(ids, (set, frozenset, list, tuple)) else frozenset()
        except Exception:  # noqa: BLE001 -- a failed probe must never break the run
            return frozenset()

    def _record_followup_decision(self, task_id: str, decision: FollowUpDecision, *,
                                  done: int, limit: int) -> None:
        """Log WHY a follow-up was or was not started, and put it on the task's feed when it matters."""
        log.info("task %s auto follow-up (%d of %d used): %s -> %s", task_id, done, limit,
                 "continue" if decision.follow_up else "stop", decision.reason)
        interesting = decision.follow_up or bool(decision.remaining) or decision.reason.startswith(
            ("blocked", "cap reached", "token budget", "no remaining"))
        if not interesting:
            return
        if decision.follow_up:
            text = (f"Continuing automatically (follow-up run {done + 1} of {limit}): "
                    f"{len(decision.remaining)} item(s) still open.")
        else:
            text = "Not continuing automatically: " + decision.reason + "."
        self._report_progress(task_id, "status", text=text, data={"auto_followup": {
            "continued": decision.follow_up, "reason": decision.reason,
            "follow_up_runs_made": done, "follow_up_run_limit": limit,
            "remaining": list(decision.remaining)}})

    def _follow_up_while_incomplete(self, result: OrchestratorResult, task: Dict[str, Any],
                                    task_id: str, run_once: Callable[..., OrchestratorResult], *,
                                    ask: str, quest_id: Optional[str], run_started: float,
                                    resume_session_id: Optional[str], supports_deep_goal: bool,
                                    decisions_before: FrozenSet[str] = frozenset()
                                    ) -> OrchestratorResult:
        """``result``, or the result of the last follow-up run when the ask was clearly unfinished.

        Each pass asks ``assess`` about the latest result. A follow-up is another ``Orchestrator.run``
        on the same task, resuming the previous run's session and sent straight to a deep run
        (``deep_goal``) when the brain supports it. A paused run, a usage limit or a provider outage
        is left to the caller's own handling, and a follow-up that fails keeps the previous result
        (its work is real) rather than turning the task into a failure.
        """
        limit, budget = self._follow_up_limits()
        done = 0
        tokens = result_tokens(result)
        while True:
            if (self.paused_deep_results(result) or self._usage_limit_behind(result, run_started)
                    or self._provider_outage_behind(result, run_started)):
                return result
            new_decisions = self._open_decision_ids(quest_id) - decisions_before
            decision = assess(result, followups_done=done, max_followups=limit, tokens_used=tokens,
                              token_budget=budget, new_decision_ids=new_decisions)
            if decision.follow_up and self._is_task_cancelled(task_id):
                decision = FollowUpDecision(False, "blocked: the task was cancelled", decision.remaining)
            self._record_followup_decision(task_id, decision, done=done, limit=limit)
            if not decision.follow_up:
                return result
            brief = followup_brief(ask, result, decision.remaining, number=done + 1, limit=limit)
            extra = ({"deep_goal": "Finish what remains of the original task: "
                      + " ".join(ask.split())[:_GOAL_ASK_CHARS]} if supports_deep_goal else None)
            resume = terminal_session_id(result) or resume_session_id
            done += 1
            try:
                nxt = run_once(brief, resume, extra)
            except Exception as e:  # noqa: BLE001
                # A usage limit or provider outage is a wait the caller already knows how to hand
                # back to the queue; anything else keeps the work the earlier run really did.
                if usage_limit.from_exception(e) is not None or provider_outage.from_exception(e) is not None:
                    raise
                log.warning("task %s auto follow-up run %d failed, keeping the earlier result: %s",
                            task_id, done, e)
                self._report_progress(task_id, "status", text=(
                    f"The automatic follow-up run failed ({type(e).__name__}); keeping the result "
                    "of the earlier run."))
                return result
            tokens += result_tokens(nxt)
            result = nxt
