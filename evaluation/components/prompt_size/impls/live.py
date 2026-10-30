"""CURRENT: the live code path at each assembly point, driven offline.

* planner: the real ``Orchestrator._plan`` on the offline orchestrator, with the model provider replaced by a
  capture that records the prompt and returns nothing (the harness latch stays: no deep runner, no Quest).
* autopilot_batch: ``runner.autopilot_briefs.compose_batch_text`` (fitted to ``AUTOPILOT_BRIEF_TOKEN_BUDGET``).
* context_update_bundle: ``ContextUpdates.as_prompt_block``.
* deep_prompt: ``core.goal_runner_contract.compose_goal_prompt`` (fitted by ``core.prompt_budget``).
Zero model calls.
"""
import common


class _Capture:
    """Stands in for the model provider: keeps the prompt it was handed, decides nothing."""

    def __init__(self):
        self.prompt = ""

    def plan(self, prompt, *, model, tool_schema):  # noqa: ARG002  (no ``layers``: the flat prompt is the faithful one)
        self.prompt = prompt
        return {}


def build(meter, profile="full"):
    orch = common.build_offline_orchestrator(meter)
    cap = _Capture()
    orch.get_provider_for_model = lambda model: cap

    def assemble(point, inputs):
        if point in ("planner", "planner_situation"):
            facts = inputs.get("situation") or {}   # a planner_situation case names the turn's facts; "profile" overrides the arm's
            orch.cfg.planner_prompt_profile = inputs.get("profile", profile)
            orch._plan(inputs["user_message"], inputs["transcript"], inputs["context_view"], inputs["gathered"],
                       brainstorm=facts.get("mode", "normal") != "normal", user_vetoed_task=facts.get("mode") == "hold_off",
                       turn_facts=facts)
            return cap.prompt
        if point == "autopilot_batch":
            from quest_ai_runner.runner.autopilot_briefs import compose_batch_text
            return compose_batch_text(**inputs)
        if point == "context_update_bundle":
            return inputs["bundle"].as_prompt_block()
        if point == "deep_prompt":
            from quest_ai_runner.core.goal_runner_contract import compose_goal_prompt
            return compose_goal_prompt(inputs["goal"], inputs["brief"], preamble=inputs["preamble"])
        raise ValueError(f"unknown assembly point: {point}")
    return assemble
