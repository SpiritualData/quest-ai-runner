"""The hub's own tool loop as a deep runner, QAR's binding of the hub's (``hubofhubs.api.SolutionLoopRunner``).

The loop, its action protocol, the command sandbox, session resume and steering are the hub's. ``HubLoopRunner`` here is the hub's
class with QAR's settings (``goal_runner_contract.goal_settings``), named ``hub_loop``. It is on trial: the hub never hands it out as
its pick, so a lane runs it only when its configuration names it (``selections["delegated-agent-sessions"] = "hub_loop"``, or the
``ESOI_SELECT_QUEST_AI_RUNNER_DELEGATED_AGENT_SESSIONS`` variable).
"""
from __future__ import annotations

from esoi import implementation

from hubofhubs.api import GoalSettings, SolutionLoopRunner as HubSolutionLoopRunner

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig, goal_settings


@implementation("delegated-agent-sessions", "hub_loop")
class HubLoopRunner(HubSolutionLoopRunner):
    """Deep runner that works a goal with the hub's own loop of pure completions and hub-run actions (with QAR's settings)."""

    config_class = SubprocessConfig

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time (QAR is the consumer that owns its settings)."""
        return goal_settings()
