"""The one QAR-specific piece of the component evaluations: a real QAR Orchestrator with NO deep runner and NO Quest server.

The safety latch (``cfg.deep_runner = None`` plus the unroutable Quest URL in ``common``) means an evaluation can only ever call the
component under test. It lives apart from ``common`` so that ``common`` stays a foundation that depends on no solution.
"""
import os
import tempfile

import common


def build_offline_orchestrator(meter, registry=None, planner_tier="haiku", recipe_store=None, selections=None):
    """Real Orchestrator, deep runner disabled, no Quest server. Refuses to return if a deep runner is wired.
    ``recipe_store`` wires a RecipeStore; None leaves recipes off (QAR_RECIPES is scrubbed).
    ``selections`` overrides the project default for this orchestrator (``RunnerConfig.selections``); every
    component measures the planner alone, the only route."""
    common.require_anthropic()
    scratch = tempfile.mkdtemp(prefix="qar_comp_")
    os.environ.update(QUEST_BASE_URL=common.OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval",
                      QAR_STANDARD_TOOLS="1", QAR_CORPUS_ROOT=scratch, QAR_CONVERSATION_SEARCH="false",
                      QAR_MODEL_BACKEND=os.environ.get("QAR_MODEL_BACKEND", "claude_cli"),
                      QAR_PLANNER_TIER=planner_tier, QUEST_AI_RECENT_CONTEXT_DISABLED="1",
                      QUEST_AI_ANTICIPATION="off")
    for k in common.SCRUB:
        os.environ.pop(k, None)
    from quest_ai_runner.the_consciousness.terminal.cli import _config_from_env
    from quest_ai_runner.the_body.configuration.config import build_orchestrator
    cfg = _config_from_env()
    cfg.selections = {**(cfg.selections or {}), **(selections or {})}
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
