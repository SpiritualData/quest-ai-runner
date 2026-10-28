"""CURRENT: the planner's view of what was gathered (``planner_context._render_gathered_for_planner`` with the
orchestrator's default window): the newest few observations in full, older ones in one line of at most 160
characters, every one admitted. No goal-relevance gate and no scope fence at this point (the fence is upstream, in the
retrieval adapters). Zero model calls."""
import common  # noqa: F401
from quest_ai_runner.core.orchestrator_types import OrchestratorConfig
from quest_ai_runner.core.planner_context import _render_gathered_for_planner


def build(meter):  # noqa: ARG001
    cfg = OrchestratorConfig()

    def admit(case):
        gathered = [{"kind": "read", "rel_path": i["rel_path"], "locator": "head", "text": i["text"]}
                    for i in case["items"]]
        return {"view": _render_gathered_for_planner(gathered, cfg.planner_recent_full, cfg.planner_compress_over, 3)}
    return admit
