"""The situational planner profile: each turn's prompt carries only the rules that apply to that turn.

``planner_prompt.PLANNER_RULES`` is the doctrine as data and ``hubofhubs.doctrine`` the generic assembler. Here: the typed
situation, built from structured state the orchestrator holds, never from words the model wrote (``asks_change`` is the
planner's own ``user_intent`` verdict, so step 0 does not know it), and None for a fact the turn lacks, which keeps the
rule. Opt in with ``planner_prompt_profile="situational"``; the default stays "full".
"""
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping, Optional

from esoi import implementation
from hubofhubs.doctrine import Doctrine, assemble_doctrine

from .planner_prompt import PLANNER_RULES

SITUATIONAL_PROFILE = "situational"


@dataclass(frozen=True)
class PlannerSituation:
    mode: str = "normal"                      # "normal" | "brainstorm" | "hold_off": the latter two suspend deep and confirm
    deep_available: Optional[bool] = None     # a deep runner is wired (None: none known, which is not "none")
    run_in_progress: Optional[bool] = None    # the consumer told us a run of this conversation is in flight
    reach: Optional[str] = None               # the reach judge's verdict: "inside" | "outside" | "world"
    tools_offered: Optional[bool] = None      # direct tools are offered (their block is appended apart from the rules)
    asks_change: Optional[bool] = None        # the planner's own verdict on the message: True only for "act"
    has_material: Optional[bool] = None       # context or gathered reads exist for the answer to be grounded in


FACTS = tuple(f.name for f in fields(PlannerSituation))


def planner_situation(*, brainstorm: bool = False, user_vetoed_task: bool = False, deep_available: Optional[bool] = None,
                      tools_offered: Optional[bool] = None, reach: Optional[str] = None,
                      has_material: Optional[bool] = None, turn_facts: Optional[Mapping[str, Any]] = None
                      ) -> PlannerSituation:
    """The situation of one planner step. ``turn_facts`` is what the run already knows: ``user_intent`` (the verdict of an
    earlier step), ``runs_in_flight`` (a count from the consumer's context meta), or any situation field outright."""
    facts = dict(turn_facts or {})
    intent, runs = facts.pop("user_intent", None), facts.pop("runs_in_flight", None)
    base = PlannerSituation(
        mode="hold_off" if brainstorm and user_vetoed_task else "brainstorm" if brainstorm else "normal",
        deep_available=deep_available, run_in_progress=None if runs is None else runs > 0, reach=reach,
        tools_offered=tools_offered, asks_change=None if not intent else intent == "act", has_material=has_material)
    return replace(base, **{k: v for k, v in facts.items() if k in FACTS})


@implementation("assemble-a-prompt", "situational")
def situational_template(situation: PlannerSituation) -> Doctrine:
    """The planner prompt template for ``situation``: the rules that apply, in order, still ``.format``-able."""
    return assemble_doctrine(PLANNER_RULES, {name: getattr(situation, name) for name in FACTS})
