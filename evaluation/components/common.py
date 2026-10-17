"""Shared core for the per-component QAR evaluations. Nothing here knows about a particular solution.

The measuring machinery (call meter, case loading, sampling, recording, the leaderboard) is the ``esoi`` package;
this module re-exports what the components and their tests import and adds the one QAR-specific piece:
``build_offline_orchestrator``, a real QAR Orchestrator with NO deep runner and NO Quest server (the safety
latch: ``cfg.deep_runner = None`` plus an unroutable Quest URL), so an evaluation can only ever call the
component under test. Run a component with ``esoi run <component> --impl <name>``.
"""
import os
import sys
import tempfile
from pathlib import Path

import esoi.config
import esoi.hermetic
from esoi import harness
from esoi.harness import BudgetExceeded, CallMeter, stratified_sample  # noqa: F401  (re-exported)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

CONFIG = esoi.config.load(REPO)

OFFLINE_QUEST_URL = "http://quest.invalid"  # never resolves: nothing can reach a Quest server
SCRUB = ("QAR_TOOLS_FILE", "QAR_CONFIG_FILE", "QAR_CONTEXT_PREAMBLE_FILE", "QAR_LINK_POLICY_FILE",
         "QAR_MODEL_FAST", "QAR_MODEL_BALANCED", "QAR_MODEL_QUALITY", "QAR_MODEL_BEST",
         "QAR_OVERSEER_TIER", "QAR_VERIFY_TIER", "QAR_OVERSEER", "QUEST_TEAM_ID", "QAR_RECIPES")


def hermetic_env(extra=None):
    """Scrub the variables ``[hermetic]`` in esoi.toml names for the block, set ``extra`` on top, restore on exit."""
    return esoi.hermetic.from_config(CONFIG, extra)


def load_impl(spec, component):
    """``--impl name`` -> ``<component>/impls/name.py``'s ``build(meter)``; ``module:function`` is also accepted."""
    return harness.load_impl(spec, component)


def load_cases(component):
    """The component's cases (see ``esoi.harness.load_cases``)."""
    return harness.load_cases(component, CONFIG)


def build_offline_orchestrator(meter, registry=None, planner_tier="haiku", recipe_store=None, selections=None):
    """Real Orchestrator, deep runner disabled, no Quest server. Refuses to return if a deep runner is wired.
    ``recipe_store`` wires a RecipeStore; None leaves recipes off (QAR_RECIPES is scrubbed).
    ``selections`` overrides the selection table for this orchestrator (``RunnerConfig.selections``); every
    component measures the planner alone (``operate-a-quest`` = ``orchestrator``) unless it asks otherwise."""
    scratch = tempfile.mkdtemp(prefix="qar_comp_")
    os.environ.update(QUEST_BASE_URL=OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval",
                      QAR_STANDARD_TOOLS="1", QAR_CORPUS_ROOT=scratch, QAR_CONVERSATION_SEARCH="false",
                      QAR_MODEL_BACKEND=os.environ.get("QAR_MODEL_BACKEND", "claude_cli"),
                      QAR_PLANNER_TIER=planner_tier, QUEST_AI_RECENT_CONTEXT_DISABLED="1",
                      QUEST_AI_ANTICIPATION="off")
    for k in SCRUB:
        os.environ.pop(k, None)
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator
    cfg = _config_from_env()
    cfg.selections = {"operate-a-quest": "orchestrator", **(cfg.selections or {}), **(selections or {})}
    cfg.deep_runner = None  # THE LATCH: explicit None = execution disabled (config.resolve_deep_runner)
    if registry is not None:
        cfg.tool_registry = registry
    if recipe_store is not None:
        cfg.recipe_store = recipe_store
    orch = build_orchestrator(cfg)
    if getattr(orch, "deep_runner", None):
        raise SystemExit("deep runner is wired; refusing to run")
    meter.wrap(orch.provider)
    for p in getattr(orch, "providers", {}).values() if isinstance(getattr(orch, "providers", None), dict) else []:
        meter.wrap(p)
    return orch
