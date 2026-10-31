"""The pre-flight plan check: is the planned approach able to reach the evidence that would show the work done?

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.

Runs ONLY for a goal the planner classified ``work_shape == "analysis"`` (and only with
``OrchestratorConfig.done_evidence_enabled``), so a lookup, an action or a conversation turn pays nothing: no
call, no prompt text. One balanced-tier call, before the first attempt, compares the brief with the requirements the
planner wrote (``PlanDecision.done_evidence``) and with the consumer's statement of what its data and code paths
cannot do (``OrchestratorConfig.consumer_limits``). Three outcomes, each already supported by the loop:

  * sufficient: run as planned, with the requirements appended to the brief.
  * improvable: run on ``revised_brief`` (count first, work in batches, use a method that fits).
  * the person must settle something: ``question`` becomes the turn's reply (the run does not start); or the
    standard cannot be reached in one turn: ``honest_scope`` is appended to the brief so the final reply states it.

The shape is the hub's sufficiency idea (``hubofhubs.critique.sufficiency``): a structural declaration the loop gates
on. The call itself is a QAR prompt (``preflight.plan-sufficiency``). A call that fails or returns nothing usable
changes nothing: the run proceeds as planned. Never raises.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hubofhubs.critique.done_evidence import render_requirements
from hubofhubs.ports.model import STEP_JUDGE, plan_with_step

from .auxiliary_prompts import PREFLIGHT_PLAN_TOOL
from .card_filter import _extract_json
from .orchestrator_constants import log
from .prompt_specs import render_prompt

#: Appended to the brief of an analysis, so the worker is held to the same requirements the verifier checks.
BRIEF_REQUIREMENTS_HEAD = "THE WORK IS DONE ONLY WHEN (checked after you finish):"
BRIEF_SCOPE_HEAD = "STATE THIS SCOPE IN YOUR RESULT:"


@dataclass
class PreflightOutcome:
    """What the pre-flight decided. ``brief`` is the brief to run with; ``question`` set means do not run."""
    brief: str
    question: str = ""
    called: bool = False
    gaps: List[str] = field(default_factory=list)
    honest_scope: str = ""


def brief_with_requirements(brief: str, requirements: List[str], scope: str = "") -> str:
    """``brief`` followed by the numbered requirements (and the honest scope, when there is one)."""
    out = brief
    block = render_requirements(requirements)
    if block:
        out = f"{out}\n\n{BRIEF_REQUIREMENTS_HEAD}\n{block}"
    if scope:
        out = f"{out}\n\n{BRIEF_SCOPE_HEAD} {scope}"
    return out


class PreflightMixin:
    """The pre-flight plan check for an analysis goal."""

    def preflight_applies(self, plan: Any) -> bool:
        """True when the pre-flight runs for ``plan``: opted in, and the planner called the work an analysis."""
        return bool(getattr(self.cfg, "done_evidence_enabled", False)
                    and getattr(plan, "work_shape", None) == "analysis")

    def _preflight_plan(self, plan: Any, goal: str, brief: str) -> PreflightOutcome:
        """The plan check for one analysis goal. Returns the brief to run with, or a question to put to the person."""
        requirements = list(getattr(plan, "done_evidence", None) or [])
        with_evidence = brief_with_requirements(brief, requirements)
        outcome = PreflightOutcome(brief=with_evidence)
        limits = (getattr(self.cfg, "consumer_limits", "") or "").strip()
        verdict = self._preflight_verdict(goal, brief, requirements, limits)
        if verdict is None:
            return outcome
        outcome.called = True
        if verdict.get("sufficient"):
            return outcome
        outcome.gaps = [str(g).strip() for g in (verdict.get("gaps") or []) if str(g).strip()]
        question = str(verdict.get("question") or "").strip()
        if question:
            outcome.question = question
            return outcome
        revised = str(verdict.get("revised_brief") or "").strip()
        scope = str(verdict.get("honest_scope") or "").strip()
        outcome.honest_scope = scope
        outcome.brief = brief_with_requirements(revised or brief, requirements, scope)
        return outcome

    def _preflight_verdict(self, goal: str, brief: str, requirements: List[str],
                           limits: str) -> Optional[Dict[str, Any]]:
        """The model's plan-sufficiency verdict as a dict, or None when it could not be had."""
        try:
            model = self.registry.resolve_tier(self.cfg.planner_tier)
            provider = self.get_provider_for_model(model)
            prompt = render_prompt(
                "preflight.plan-sufficiency", goal=(goal or "")[:1000],
                evidence=render_requirements(requirements) or "(none written)",
                limits=limits or "(none stated)", brief=(brief or "")[:3000])
            raw = plan_with_step(provider, prompt, step=STEP_JUDGE, model=model,
                                 tool_schema=PREFLIGHT_PLAN_TOOL)
            if isinstance(raw, str):
                raw = json.loads(_extract_json(raw) or "{}")
            return raw if isinstance(raw, dict) and "sufficient" in raw else None
        except Exception as e:  # noqa: BLE001 -- the check must never break the run
            log.warning("pre-flight plan check failed (%s: %s); running the plan as written",
                        type(e).__name__, e)
            return None
