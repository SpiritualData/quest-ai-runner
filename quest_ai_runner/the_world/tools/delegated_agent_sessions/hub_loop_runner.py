"""The hub's own tool loop as a deep runner, QAR's binding of the hub's (``hubofhubs.api.SolutionLoopRunner``).

The loop, its action protocol, the command sandbox, session resume and steering are the hub's. ``HubLoopRunner`` here is the hub's
class with QAR's settings (``goal_runner_contract.goal_settings``), named ``hub_loop``. It is on trial: the hub never hands it out as
its pick, so a lane runs it only when its configuration names it (``selections["delegated-agent-sessions"] = "hub_loop"``, or the
``ESOI_SELECT_QUEST_AI_RUNNER_DELEGATED_AGENT_SESSIONS`` variable).
"""
from __future__ import annotations

from esoi import implementation

import json
import os
from typing import Any, Dict, Mapping, Optional

from hubofhubs.api import GoalSettings, SolutionLoopRunner as HubSolutionLoopRunner, Tool

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig, goal_settings


@implementation("delegated-agent-sessions", "hub_loop")
class HubLoopRunner(HubSolutionLoopRunner):
    """Deep runner that works a goal with the hub's own loop of pure completions and hub-run actions (with QAR's settings)."""

    config_class = SubprocessConfig

    def __init__(self, config: Optional[SubprocessConfig] = None, **arguments: Any):
        arguments.setdefault("tools", web_tools(os.environ) or None)
        super().__init__(config, **arguments)

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time (QAR is the consumer that owns its settings)."""
        return goal_settings()


def web_tools(env: Mapping[str, str]) -> Dict[str, Tool]:
    """The live web as tools for the loop, when this deployment has a search backend configured (else none): ``web_search`` takes a query,
    ``read_page`` takes a URL, optionally followed by a space and what to look for in the page. Both go through QAR's web research adapter, so its
    cache and daily limit of paid calls apply."""
    try:
        from quest_ai_runner.the_world.tools.web_search.web_research import build_web_research_from_env
        research = build_web_research_from_env(env)
    except Exception:  # noqa: BLE001 -- a deployment without web search still runs deep work
        research = None
    if research is None:
        return {}

    def text_of(observation: Any) -> str:
        if getattr(observation, "error", None):
            return f"error: {observation.error}"
        hits = getattr(observation, "hits", None) or []
        return getattr(observation, "text", None) or json.dumps(hits, ensure_ascii=False)[:6000] or "(nothing found)"

    def search(query: str) -> str:
        return text_of(research.search(query.strip()))

    def read(argument: str) -> str:
        url, _, focus = argument.strip().partition(" ")
        return text_of(research.fetch(url, focus=focus.strip() or None))

    return {"web_search": Tool("search the live web for a query; returns titles, links and snippets", search),
            "read_page": Tool("read one web page: the URL, then optionally a space and what to look for in it", read)}
