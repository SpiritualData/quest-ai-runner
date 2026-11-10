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
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from hubofhubs.api import GoalSettings, SolutionLoopRunner as HubSolutionLoopRunner, Tool

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig, goal_settings


@implementation("delegated-agent-sessions", "hub_loop")
class HubLoopRunner(HubSolutionLoopRunner):
    """Deep runner that works a goal with the hub's own loop of pure completions and hub-run actions (with QAR's settings)."""

    config_class = SubprocessConfig

    def __init__(self, config: Optional[SubprocessConfig] = None, **arguments: Any):
        arguments.setdefault("tools", {**skill_tools(os.environ), **web_tools(os.environ)} or None)
        super().__init__(config, **arguments)

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time (QAR is the consumer that owns its settings)."""
        return goal_settings()


def claude_login_research(env: Mapping[str, str]) -> Any:
    """A web research adapter that searches through the Claude Code login (QAR's Claude CLI provider), for a lane with no search backend; None
    when web search is disabled (``WEB_SEARCH_ENABLED``) or the adapter cannot be built."""
    try:
        from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider
        from quest_ai_runner.the_world.tools.web_search.web_research import build_web_research_from_env
        from quest_ai_runner.the_world.tools.web_search.web_search_backends import QAR_KEYS
        return build_web_research_from_env({**env, QAR_KEYS.backend: "claude_login"}, provider=ClaudeCliProvider())
    except Exception:  # noqa: BLE001 -- a lane that cannot build it still runs deep work
        return None


def web_tools(env: Mapping[str, str]) -> Dict[str, Tool]:
    """The live web as tools for the loop: ``web_search`` takes a query, ``read_page`` takes a URL, optionally followed by a space and what to
    look for in the page. Both go through QAR's web research adapter, so its cache and daily limit of paid calls apply. The adapter uses the
    lane's search backend when one is configured, else the Claude Code login (one agent process per search); none when web search is disabled."""
    try:
        from quest_ai_runner.the_world.tools.web_search.web_research import build_web_research_from_env
        research = build_web_research_from_env(env)
    except Exception:  # noqa: BLE001 -- a deployment without web search still runs deep work
        research = None
    if research is None:
        research = claude_login_research(env)
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


def skills_folder(env: Mapping[str, str]) -> Optional[Path]:
    """The folder of this lane's skills (one folder per skill holding ``SKILL.md``): ``QAR_SKILLS_DIR`` when set, else ``.claude/skills`` under
    the home folder; None when there is none."""
    named = env.get("QAR_SKILLS_DIR") or ""
    folder = Path(named).expanduser() if named else Path(env.get("HOME") or Path.home()) / ".claude" / "skills"
    return folder if folder.is_dir() else None


def skill_tools(env: Mapping[str, str]) -> Dict[str, Tool]:
    """The lane's skills as tools for the loop (none when the lane has no skills folder). The external agent this runner stands in for loads
    skills by itself; the loop is handed them: ``list_skills`` gives each skill's name and what it is for, ``read_skill`` gives one skill's text."""
    folder = skills_folder(env)
    if folder is None:
        return {}

    def described(text: str) -> str:
        for line in text.splitlines()[:12]:
            if line.lower().startswith("description:"):
                return " ".join(line.split(":", 1)[1].split())[:200]
        return ""

    def listed(_argument: str) -> str:
        lines = []
        for path in sorted(folder.iterdir()):
            try:
                lines.append(f"{path.name}: {described((path / 'SKILL.md').read_text(encoding='utf-8', errors='replace'))}")
            except OSError:
                continue
        return "\n".join(lines) or "(no skills)"

    def read(name: str) -> str:
        name = name.strip().strip("/")
        if not name or "/" in name or name.startswith("."):
            return "error: give one skill name from list_skills"
        try:
            return (folder / name / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            return f"error: no skill named {name!r}; use list_skills"

    return {"list_skills": Tool("list this lane's skills (runbooks for kinds of task) with what each is for; look before doing a kind of task "
                                "the goal or context ties to a skill, a person or a system", listed),
            "read_skill": Tool("read one skill by name (from list_skills) and follow it", read)}
