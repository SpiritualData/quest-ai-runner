"""The hub's framed session with sub-agents scoped to solutions as a deep runner, QAR's binding of the hub's (``hubofhubs.api.SolutionAgentsNativeRunner``).

``SolutionAgentsLoopRunner`` is the hub's class with QAR's settings (``goal_runner_contract.goal_settings``), named ``solution_agents_loop``. It is the framed
Claude Code session of ``native_loop`` (same ``lane_note`` for the lane's own operations, same tools) with three things the lane hands it:

* ``skills_dir``: the lane's skills folder (``skills_folder``), so the frame carries a real skills index (name, what it is for, path) and not the plain sentence
  ``native_loop`` writes (``skills_note`` is not used here: the index replaces it);
* ``solutions``: the solutions of the task's own quest specification, read from Quest at run time, per run. The sub-agents of a task are then the solutions of that
  task's quest (id, name, purpose, approach, out of scope, next unmet milestone, children by ``delegates_to``, role ``gather``, ``work`` or ``review`` when the record
  names one). A quest with no solutions, a run whose quest is not known, or a specification that cannot be read gives nothing, and the hub's own fallback applies
  (the working folder's records, else its small fixed set of work solutions);
* no ``recall``: the deep run's brief already carries the lane's retrieved context (the run's context preamble is part of the frame), so a second retrieval would
  only repeat it.

Where the quest is known: the runner is built once and runs many tasks, so the quest is per run. ``run_goal`` takes ``quest_id`` (the deep run passes ``ctx_meta["quest_id"]``
to a runner that names it); when it is absent the brief's sentence ("This run's quest is <id>", written when the lane wires tools) is read instead. The runner holds the
quest in a thread-local for the length of the run only; a run with neither gets the hub's fallback.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional

from esoi import implementation

from hubofhubs.api import GoalSettings, SolutionAgentsNativeRunner as HubSolutionAgentsNativeRunner, SolutionSpec

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig, SubprocessGoalRunner, goal_settings
from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import skills_folder
from quest_ai_runner.the_world.tools.delegated_agent_sessions.native_loop_runner import lane_note

QUEST_IN_BRIEF = re.compile(r"This run's quest is (quest_[A-Za-z0-9]+)")
MAX_SOLUTIONS = 6                      # the hub offers at most this many agents
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
ROLES = ("gather", "work", "review")


def quest_of(text: str) -> str:
    """The quest id a deep run's brief names, or "" when it names none."""
    found = QUEST_IN_BRIEF.search(text or "")
    return found.group(1) if found else ""


def quest_solution_rows(spec: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{solution id: row}`` of a quest specification as Quest returns it (``records``, each with ``type``, ``recordId`` and ``fields``): every field of the record, plus
    ``milestones`` set to the next milestone not yet met (the hub reads the first unmet one as the agent's done-standard), "" when all are met."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_ladder import next_unmet_milestone
    rows: Dict[str, Dict[str, Any]] = {}
    for record in spec.get("records") or []:
        if str(record.get("type")) != "solution" or not record.get("recordId"):
            continue
        fields = dict(record.get("fields") or {})
        waiting = next_unmet_milestone({**fields, "milestoneEntries": record.get("milestoneEntries")})
        waiting = re.sub(r"^\s*1\.\s+", "", waiting)          # a text cell already numbered keeps one number
        fields["milestones"] = f"1. {waiting}" if waiting else ""
        rows[str(record["recordId"])] = fields
    return rows


def quest_network(quest_id: str, rows: Mapping[str, Mapping[str, Any]]) -> Any:
    """The hub's network of the quest's solutions (built from rows, no folder)."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.solution_consideration import quest_network as network
    return network(quest_id, {"solutions": dict(rows)})


def solution_specs(network: Any, rows: Mapping[str, Mapping[str, Any]], goal: str, limit: int = MAX_SOLUTIONS) -> List[Any]:
    """The quest's solutions as the hub's specs: those the goal concerns first (the network's own relevance), then the rest in record order, at most ``limit``. A row's
    ``role`` of ``gather``, ``work`` or ``review`` picks the agent's tier and tools (read-only tools for gather and review); no role is ``work``."""
    try:
        hits = [str(hit.get("solution_id") or "") for hit in network.classify(goal, limit)]
    except Exception:  # noqa: BLE001 -- a relevance that fails leaves the record order
        hits = []
    order = [s for s in dict.fromkeys(hits) if s in rows] + [s for s in rows if s not in hits]
    specs = []
    for sid in order[:limit]:
        role = str(rows[sid].get("role") or "").strip().lower()
        kind = role if role in ROLES else "work"
        specs.append(SolutionSpec(sid, kind, READ_ONLY_TOOLS) if kind != "work" else SolutionSpec(sid, kind))
    return specs


@implementation("delegated-agent-sessions", "solution_agents_loop")
class SolutionAgentsLoopRunner(HubSolutionAgentsNativeRunner, SubprocessGoalRunner):
    """Deep runner that works a goal in one hub-framed Claude Code session whose sub-agents are the solutions of the task's own quest (with QAR's settings)."""

    config_class = SubprocessConfig

    def __init__(self, config: Optional[SubprocessConfig] = None, *, client_factory: Optional[Callable[[], Any]] = None,
                 provider: str = "anthropic", tier: str = "", model_translator: Any = None, **arguments: Any):
        self.client_factory = client_factory        # provider, tier and model_translator are named so the hub's pick (resolve_hub_pick) can pass them
        arguments.update(provider=provider, tier=tier, model_translator=model_translator)
        self.view = threading.local()                       # the quest of the run being prepared, per thread
        folder = skills_folder(os.environ)
        arguments.setdefault("notes", lane_note(os.environ))
        if folder is not None:
            arguments.setdefault("skills_dir", str(folder))
        arguments.setdefault("solutions", self.quest_solutions)
        super().__init__(config, **arguments)

    @property
    def records(self) -> Any:
        """The quest's network while a run is prepared (so the agents are read from the quest's records), else the one the hub was given."""
        return getattr(self.view, "network", None) or self.default_records

    @records.setter
    def records(self, value: Any) -> None:
        self.default_records = value

    def client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        from quest_ai_runner.the_body.internal_context.quest.client.quest_client import quest_client_from_env
        return quest_client_from_env(os.environ)

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None, emit: Any = None,
                 context_preamble: Optional[str] = None, run_id: Optional[str] = None, working_dir: Optional[str] = None,
                 resume_session_id: Optional[str] = None, requester_user_id: Optional[str] = None, cancel_check: Any = None, control: Any = None,
                 quest_id: Optional[str] = None) -> Any:
        """The hub's ``run_goal`` plus the run's quest id as data (the deep run passes it to a runner that names it). Held for this thread's run only; ``prepare`` reads
        it and falls back to the brief's sentence when it is absent. Spelled out, not ``**kwargs``, because the deep run offers a runner only what it declares."""
        self.view.quest_id = quest_id or ""
        try:
            return super().run_goal(goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit, context_preamble=context_preamble, run_id=run_id,
                                    working_dir=working_dir, resume_session_id=resume_session_id, requester_user_id=requester_user_id,
                                    cancel_check=cancel_check, control=control)
        finally:
            self.view.quest_id = ""

    def prepare(self, goal: str, brief: str, preamble: str, root: str, session_key: str) -> None:
        self.view.network, self.view.rows = None, {}
        try:
            try:
                quest_id = getattr(self.view, "quest_id", "") or quest_of(brief or goal)
                spec = self.client().get_spec("quest", quest_id) if quest_id else None
                rows = quest_solution_rows(spec) if spec else {}
                if rows:
                    self.view.network, self.view.rows = quest_network(quest_id, rows), rows
            except Exception:  # noqa: BLE001 -- a quest that cannot be read leaves the hub's own choice
                self.view.network, self.view.rows = None, {}
            # Once, outside the read's guard: a failure inside the hub's own prepare is its own error, never a reason to run it again.
            super().prepare(goal, brief, preamble, root, session_key)
        finally:
            self.view.network, self.view.rows = None, {}

    def quest_solutions(self, goal: str, root: str) -> List[Any]:
        """The hub's ``solutions`` callable: the specs of the quest being prepared, [] when it has none (the hub's fallback then applies)."""
        rows = getattr(self.view, "rows", None) or {}
        network = getattr(self.view, "network", None)
        return solution_specs(network, rows, goal) if rows and network is not None else []

    def goal_settings(self) -> GoalSettings:
        """QAR's goal settings, read from the environment at call time."""
        return goal_settings()
