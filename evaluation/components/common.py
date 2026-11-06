"""Shared core for the per-component QAR evaluations. Nothing here knows about a particular solution.

The measuring machinery (call meter, case loading, sampling, recording, the leaderboard) is the ``esoi`` package;
this module re-exports what the components and their tests import and holds the unroutable Quest URL
the offline orchestrator (``offline_orchestrator.py``) uses as its safety latch. Run a component with ``esoi run <component> --impl <name>``.
"""
import os
import sys
from pathlib import Path

import esoi
from esoi import harness
from esoi import BudgetExceeded, CallMeter, stratified_sample  # noqa: F401  (re-exported)

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


ANTHROPIC_BACKENDS = ("claude_cli", "anthropic")
ANTHROPIC_MODEL_WORDS = ("claude", "haiku", "sonnet", "opus", "fable", "mythos")


def require_anthropic(environ=None):
    """The organisation's decision is that metrics run on Anthropic models. A measured run refuses to start when its model backend
    or a tier override names another vendor, so a lane's leftover Gemini setting cannot quietly become the measurement. Set
    ``QAR_EVAL_ALLOW_NON_ANTHROPIC=1`` to measure a candidate on purpose (the row then says which model it used)."""
    environ = os.environ if environ is None else environ
    if environ.get("QAR_EVAL_ALLOW_NON_ANTHROPIC") == "1":
        return
    backend = environ.get("QAR_MODEL_BACKEND", "claude_cli").strip().lower()
    if backend not in ANTHROPIC_BACKENDS:
        raise SystemExit(f"metrics run on Anthropic models: QAR_MODEL_BACKEND={backend!r} is not one of {ANTHROPIC_BACKENDS} "
                         "(set QAR_EVAL_ALLOW_NON_ANTHROPIC=1 to measure another vendor on purpose)")
    for name in sorted(k for k in environ if k.startswith("QAR_MODEL_") and k != "QAR_MODEL_BACKEND"):
        value = environ[name].strip().lower()
        if value and not any(word in value for word in ANTHROPIC_MODEL_WORDS):
            raise SystemExit(f"metrics run on Anthropic models: {name}={environ[name]!r} names another vendor "
                             "(set QAR_EVAL_ALLOW_NON_ANTHROPIC=1 to measure another vendor on purpose)")
