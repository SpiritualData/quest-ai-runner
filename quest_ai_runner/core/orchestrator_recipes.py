"""Recipe fast path: recognised requests that skip the open planner.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .recipes import DEEP_PREFIX, RECIPE_ARGS_TOOL, RecipeRunner
from .adapters import (
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_RESULT,
    PlanDecision,
    ProgressEvent,
    STEP_JUDGE,
    plan_with_step,
)
from .guard import ExecutionFact, ExecutionRecord
from .tools import ToolContext
from .recent_context import quest_scope_key
from .orchestrator_turn import _Emitter
from .orchestrator_constants import log
from .orchestrator_capabilities import provider_call_accepts_tier
from .orchestrator_types import OrchestratorResult


class RecipesMixin:
    """Recipe fast path: recognised requests that skip the open planner."""

    # --- planner call --------------------------------------------------------

    # --- recipes (core/recipes.py) ---------------------------------------------------------

    def _recipe_scope_tags(self, quest_id: Optional[str],
                           context_meta: Optional[Dict[str, Any]]) -> Optional[List[str]]:
        meta = {**(context_meta or {})}
        if quest_id is not None:
            meta.setdefault("quest_id", quest_id)
        if meta.get("scope_tags"):
            return list(meta["scope_tags"])
        ids = self._meta_quest_ids(meta)
        return [quest_scope_key(q) for q in ids] if ids else None

    def _recipe_fill_args(self, prompt: str) -> Dict[str, Any]:
        """The ONE small model call a non-exact recipe replay makes (fast tier, structured)."""
        model = self.registry.resolve_tier("fast")
        provider = self.get_provider_for_model(model)
        kwargs: Dict[str, Any] = {"model": model, "tool_schema": RECIPE_ARGS_TOOL}
        if provider_call_accepts_tier(provider.plan):
            kwargs["tier"] = "fast"
        raw = plan_with_step(provider, prompt, step=STEP_JUDGE, **kwargs)
        return raw if isinstance(raw, dict) else {}

    def _recipe_fast_path(self, user_message: str, *, quest_id: Optional[str],
                          context_meta: Optional[Dict[str, Any]],
                          emit: "_Emitter") -> Optional[OrchestratorResult]:
        """Replay a known operation now, or None to take the normal path. Never raises."""
        try:
            tags = self._recipe_scope_tags(quest_id, context_meta)
            runner = RecipeRunner(self.recipes, self.tools, fill_args=self._recipe_fill_args)
            candidates = runner.nominate(user_message, tags)
            if not candidates:
                return None
            ctx_meta = {**(context_meta or {})}
            if quest_id is not None:
                ctx_meta.setdefault("quest_id", quest_id)
            emit.status("Running a saved recipe…")
            try:                                    # same per-turn counters the normal path resets
                if hasattr(self.provider, "tokens_in"):
                    self.provider.tokens_in = 0
                    self.provider.tokens_out = 0
            except Exception:  # noqa: BLE001
                pass
            outcome = runner.try_fast_path(
                user_message,
                ToolContext(quest_id=quest_id, user_id=ctx_meta.get("user_id"),
                            task_id=ctx_meta.get("task_id"), meta=ctx_meta),
                scope_tags=tags, candidates=candidates,
                allow_mutating=self.cfg.recipe_allow_mutating)
            if outcome is None:
                return None
            record = ExecutionRecord()
            if outcome.mutates:
                record.facts.append(ExecutionFact(
                    goal=f"tool {outcome.tool}: {outcome.text[:200]}", succeeded=True, failed=False,
                    error=None, phases=["tool_call", "done"]))
            res = OrchestratorResult(kind="answer", text=outcome.text, steps=0,
                                     rationale=f"recipe {outcome.recipe_id} ({outcome.tool})",
                                     exit_reason="recipe", execution_record=record)
            try:
                res.tokens_in = int(getattr(self.provider, "tokens_in", 0) or 0)
                res.tokens_out = int(getattr(self.provider, "tokens_out", 0) or 0)
            except Exception:  # noqa: BLE001
                pass
            emit.emit(ProgressEvent(type=EVENT_EXEC, step=0, text=f"tool {outcome.tool}",
                                    data={"phase": "done", "tool": outcome.tool,
                                          "recipe": outcome.recipe_id}))
            emit.emit(ProgressEvent(type=EVENT_RESULT, text=res.text, result_kind="answer",
                                    data={"exit_reason": "recipe", "recipe_id": outcome.recipe_id,
                                          "llm_calls": outcome.llm_calls}))
            emit.emit(ProgressEvent(type=EVENT_DONE, result_kind="answer", step=0))
            log.info("Recipe fast path: %s via %s in %.2fs (%d model call(s))",
                     outcome.recipe_id, outcome.tool, outcome.seconds, outcome.llm_calls)
            return res
        except Exception as e:  # noqa: BLE001 -- the fast path must never break a turn
            log.warning("Recipe fast path skipped: %s: %s", type(e).__name__, e, exc_info=True)
            return None

    def _recipe_deep_plan(self, user_message: str, *, quest_id: Optional[str],
                          context_meta: Optional[Dict[str, Any]],
                          emit: "_Emitter") -> Optional[PlanDecision]:
        """A ready ``deep`` plan for a request that repeats a saved quest-data operation, or None.

        Lexical nomination first (microseconds, no model, no index), then ONE fast-tier verdict that
        also restates the request as a self-contained goal. The caller uses the plan in place of
        the planner call and never starts the turn-start context search. Never raises.
        """
        try:
            runner = RecipeRunner(self.recipes, self.tools, fill_args=self._recipe_fill_args)
            tags = self._recipe_scope_tags(quest_id, context_meta)
            candidates = runner.nominate(user_message, tags, deep=True)
            if not candidates:
                return None
            hit = runner.deep_goal(user_message, candidates)
            if hit is None:
                return None
            recipe, goal = hit
            self.recipes.mark_used(recipe, user_message)
            emit.status("Running a saved operation…")
            log.info("Deep recipe %s matched; skipping the planner and turn-start context search",
                     recipe.id)
            return PlanDecision(action="deep", goal=goal, deep_brief=goal,
                                deep_target="quest_data",
                                rationale=f"recipe {recipe.id} ({recipe.tool})")
        except Exception as e:  # noqa: BLE001 -- the fast path must never break a turn
            log.warning("Deep recipe skipped: %s: %s", type(e).__name__, e, exc_info=True)
            return None

    def _recipe_learn_deep(self, user_message: str, plan: PlanDecision, res: Any,
                           scope_tags: Any, *, learnable: bool) -> None:
        """Save a successful quest-data deep run as a recipe. Never raises, never learns a failure."""
        try:
            if (self.recipes is None or not learnable or not self.cfg.recipe_fast_path
                    or not self.cfg.recipe_deep):
                return
            results = list(getattr(res, "deep_results", None) or [])
            # An overseer-escalated deep plan carries no deep_target, so a completed run that left a
            # write receipt through the governed runner also counts as a quest-data operation.
            if (getattr(plan, "deep_target", None) != "quest_data"
                    and not any(getattr(d, "has_write_receipt", False) for d in results)):
                return
            if not results or any(getattr(d, "deferred", False) or getattr(d, "decision_id", None)
                                  for d in results):
                return                      # only an operation that actually completed
            # Completed = every run met its goal, or the governed runner left a write receipt (the
            # goal verdict can still call a landed write unmet when the run was short on proof).
            if not (all(getattr(d, "met", False) for d in results)
                    or any(getattr(d, "has_write_receipt", False) for d in results)):
                return
            goal = (plan.goal or "").strip()
            if goal:
                self.recipes.learn(user_message, DEEP_PREFIX + "quest_data", {"goal": goal},
                                   scope_tags=scope_tags)
        except Exception:  # noqa: BLE001 -- learning a recipe never breaks a turn
            pass

    def _recipe_learner(self, user_message: str, scope_tags: Any, *,
                        learnable: bool) -> Optional[Callable[[str, Dict[str, Any]], None]]:
        """The callback that saves a successful planner tool call as a recipe (None when off).

        Only a typed user turn teaches: a queued brief or autopilot pass quotes earlier runs, so
        its text is not a request someone will type again.
        """
        if self.recipes is None or not learnable or not self.cfg.recipe_fast_path:
            return None
        store = self.recipes
        return lambda name, args: store.learn(user_message, name, args, scope_tags=scope_tags)
