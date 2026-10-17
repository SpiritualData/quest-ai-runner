"""CANDIDATE solution: the live pre-route (``core/pre_route.py``) fronts the live planner. The cheap call
settles a clear, non-destructive tool request on its own; everything else goes to the planner, which
decides exactly as in ``qar_planner``. The metric counts both calls where both ran."""
import os

import common
from classification.impls.qar_planner import decision_to_route


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    from quest_ai_runner.core.pre_route import PreRouter
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")
    orch = common.build_offline_orchestrator(meter, planner_tier=tier)
    router = PreRouter(meter.wrap(ClaudeCliProvider()), tier)

    def classify(message, context):
        router.direct_tools = frozenset(context["direct_tools"])
        pre = router.route(message, context["tools"])
        if router.settles(pre):
            return {"route": "tool", "tool": pre.calls[0]["name"], "calls": pre.calls, "raw": "pre-route"}
        quest = context["quest"]
        view = f"Quest: {quest['title']}\nOutcome: {quest['outcome']}"
        return decision_to_route(orch._plan(message, "", view, []))
    return classify
