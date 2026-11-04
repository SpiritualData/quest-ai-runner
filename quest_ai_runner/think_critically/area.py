"""``think-critically``: every request is held to the metric that serves it, and what is said is held to what is known.

The area arm ``deep_runs_only`` takes the area's members as typed seats and offers the area's one line, ``hold(case)``: the metric rows
the code kept for one request after the flow ran, by the code that holds a deep goal to its metric (``GoalMetrics`` over an in-memory
ledger and the case's true readings). The members are the way a goal iterates by its specification (``iterate-by-spec``, with the
records ``spec-selection`` picks), the judges of narrow questions (``judge-a-decision``), the guard of a reply (``guard-a-reply``), the
grounding of work in the person's intent (``ground-in-intent``), the check that a run is stalled (``detect-a-stalled-run``), the draft of
a missing metric (``draft-a-metric``) and the gates of a background pass (``gate-an-autopilot-pass``). Members the line does not need are
built on first use.
"""
from __future__ import annotations

import typing
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from esoi import implementation, solution
from esoi.assemble import Lazy
from esoi.metric_meter import AFTER, BEFORE
from esoi.spec_slice import SpecSlice
from hubofhubs import arm_needs as needs
from hubofhubs.critique import sufficiency
from hubofhubs.critique.link_detect import links_in_message
from hubofhubs.critique.link_guard import LinkGuard, LinkPolicy
from hubofhubs.deep.contract import DEEP_REVIEW_PROMPT, SubprocessConfig, parse_deep_review
from hubofhubs.deep.subprocess_runner import SubprocessGoalRunner
from hubofhubs.seats import ModelCalling

from ..runner.quest_client import QuestOperations
from ..core import quest_judge, reach_judge
from .goal_iteration import GoalIteration
from ..core.goal_metrics import GoalMetrics
from ..core.spec_selection import select_specs
from ..runner.autopilot import AutopilotPass
from ..runner.metric_draft import MetricDraft


SpecSelecting = select_specs.__wrapped__    # the function that declares the spec-selection solution, as the type of its seat


class MemoryLedger:
    """The metric ledger of one run, held in memory."""

    def __init__(self) -> None:
        self.rows: List[Any] = []

    def record(self, result: Any) -> bool:
        self.rows.append(result)
        return True

    def for_run(self, run_id: str) -> List[Any]:
        return [row for row in self.rows if row.run_id == run_id]


class CaseMeter:
    """A meter that returns one moment's true reading for the case."""
    name = "fixed"
    cheap = True

    def __init__(self, serves: Any, value: Any) -> None:
        self.serves, self.value = (serves,), value

    def measure(self, spec_slice: Any, working_dir: Optional[str] = None) -> Any:
        return self.value


class CaseIteration:
    """The iteration arm under the goal loop, with meters that read the case's true values."""
    name = "slice-and-writeback"
    measures = True

    def __init__(self, case: Mapping[str, Any]) -> None:
        self.source = self if case["has_metric"] else None
        self.meters = [CaseMeter(BEFORE, case.get("before")), CaseMeter(AFTER, case.get("after"))]

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> SpecSlice:
        return SpecSlice(root="", solution_id="s", metric_id="m", metric_name="metric")


def hold_case(case: Mapping[str, Any]) -> Dict[str, Any]:
    """What is on record for one request after the flow ran: the rows kept, whether a change ended with none, and what answered a regression.
    A chat turn and in-process work pass through nothing today, so they come back with no rows."""
    if case["kind"] != "deep":
        return {"rows": [], "missing": False, "proposed": False, "response": ""}
    metrics = GoalMetrics(CaseIteration(case), MemoryLedger(), "run1", request="r", gate_applies=True)
    metrics.begin()
    result = SimpleNamespace(has_write_receipt=bool(case["changed"]), changes_landed=0, output="done", decision_id=None)
    metrics.note_attempt(result, [])
    metrics.finish(result)
    rows = [{"before": row.get("before"), "after": row.get("after"), "source": row.get("source")} for row in result.metric_results]
    return {"rows": rows, "missing": bool(result.metric_missing), "proposed": False, "response": ""}


@solution("judge-a-decision", kind="event", entry="reach_verdict", example='{"reach": "inside", "reason": "the note is in the card"}')
@typing.runtime_checkable
class DecisionJudging(typing.Protocol):
    """Settles a narrow question from a model's raw answer: the reach of a request, the quests an item belongs to, the cards still abridged."""

    def reach_verdict(self, raw: str) -> Optional[Dict[str, Any]]: ...
    def rank(self, items: Sequence[Any], quests: Sequence[Any], call: Callable[[str, Any], str], home_quest_id: Optional[str] = None,
             fallback_order: Optional[Sequence[str]] = None) -> Any: ...
    def unfetched(self, cards: Sequence[Any], executed: Sequence[Any]) -> List[str]: ...


@implementation("judge-a-decision", "qar_judges")
class QarJudges:
    """The deterministic halves of QAR's judges: the reach judge's parse, the quest judge's ranking and the sufficiency gate's abridged check."""
    REQUIRES = ()     # no seat: the model's answer is handed in

    def reach_verdict(self, raw: str) -> Optional[Dict[str, Any]]:
        return reach_judge.parse_judge_text(raw)

    def rank(self, items: Sequence[Any], quests: Sequence[Any], call: Callable[[str, Any], str], home_quest_id: Optional[str] = None,
             fallback_order: Optional[Sequence[str]] = None) -> Any:
        return quest_judge.rank_quests(list(items), list(quests), call, home_quest_id=home_quest_id, fallback_order=fallback_order)

    def unfetched(self, cards: Sequence[Any], executed: Sequence[Any]) -> List[str]:
        return [item.label for item in sufficiency.unfetched_abridged(sufficiency.collect_abridged_items(list(cards)), list(executed))]


@solution("guard-a-reply", kind="event", entry="links", example="See https://example.org/a for details.")
@typing.runtime_checkable
class ReplyGuarding(typing.Protocol):
    """Stops an unsafe reply: finds the links in a text and checks each one, so a link that does not resolve never reaches the person."""

    def links(self, text: str) -> List[Any]: ...
    def sanitize(self, text: str, policy: Mapping[str, Any], fetcher: Callable[[str, float], Any]) -> Any: ...


@implementation("guard-a-reply", "qar_guards")
class QarGuards:
    """The link engine of the hub (``links_in_message``, ``LinkGuard``) that QAR's replies pass through."""
    REQUIRES = ()     # no seat: the fetcher is handed in

    def links(self, text: str) -> List[Any]:
        return list(links_in_message(text))

    def sanitize(self, text: str, policy: Mapping[str, Any], fetcher: Callable[[str, float], Any]) -> Any:
        return LinkGuard(LinkPolicy.from_dict(dict(policy)), fetcher=fetcher).sanitize(text)


@solution("ground-in-intent", kind="event", entry="frame", example="quest-id")
@typing.runtime_checkable
class IntentGrounding(typing.Protocol):
    """Ties work to its purpose: the frame of the person's intent a quest sits under, from the ids Quest holds."""

    def frame(self, quest_id: str) -> Optional[Mapping[str, Any]]: ...


@implementation("ground-in-intent", "frame-line")
class FrameLine:
    """The server computes a short frame from the stored ids; the quest card and the autopilot brief carry it. No model call."""
    REQUIRES = ()     # the seat it is handed

    def __init__(self, quest: QuestOperations) -> None:
        self.quest = quest

    def frame(self, quest_id: str) -> Optional[Mapping[str, Any]]:
        return self.quest.get_intent_frame(quest_id)


@solution("detect-a-stalled-run", kind="event", entry="review", example={"elapsed": "12 minutes", "idle": "9 minutes", "tail": "(no output)"})
@typing.runtime_checkable
class StallDetecting(typing.Protocol):
    """Tells a stuck run from a slow one: from the elapsed time, the idle time and the tail of the session, whether the worker should still run."""

    def review(self, elapsed: str, idle: str, tail: str) -> Mapping[str, Any]: ...


@implementation("detect-a-stalled-run", "live_reviewer")
class LiveReviewer:
    """The production reviewer: the watchdog prompt sent through one-shot ``claude -p`` and read by ``parse_deep_review``."""
    REQUIRES = (needs.CLAUDE_BINARY,)

    def review(self, elapsed: str, idle: str, tail: str) -> Mapping[str, Any]:
        runner = SubprocessGoalRunner(SubprocessConfig(working_dir=".", claude_path="claude"))
        verdict = parse_deep_review(runner.call_reviewer_cli(DEEP_REVIEW_PROMPT.format(elapsed=elapsed, idle=idle, tail=tail)))
        return {"stop": None, "reason": "unreadable verdict"} if verdict is None else {"stop": verdict[0], "reason": verdict[1]}


@solution("draft-a-metric", kind="event", entry="draft", example={"task_text": "Propose this week's value", "card": {}, "context_view": ""})
@typing.runtime_checkable
class MetricDrafting(typing.Protocol):
    """Drafts a missing metric value from the quest's own data for its owner to confirm; one light model call, capped."""

    def draft(self, task_text: str, card: Mapping[str, Any], context_view: str = "") -> Any: ...


@implementation("draft-a-metric", "capped_model_draft")
class CappedModelDraft:
    """One capped model call over the metric card and the quest's record; the model's structured answer comes back whole."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: ModelCalling, quest: QuestOperations) -> None:
        self.model, self.quest = model, quest

    def draft(self, task_text: str, card: Mapping[str, Any], context_view: str = "") -> Any:
        return MetricDraft(self.quest, self.model, "fast").decide(task_text, card, context_view)


@solution("gate-an-autopilot-pass", kind="event", entry="gate", example="quest-id")
@typing.runtime_checkable
class PassGating(typing.Protocol):
    """Keeps background work inside its hours and budget: whether a quest's pass is skipped, and why, or passes every gate to the work."""

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]: ...


@implementation("gate-an-autopilot-pass", "live_pass")
class LivePass:
    """The production autopilot pass run for one quest against the Quest account: the budget, cadence, run-now, reactive, roster-day and
    backpressure gates decide, and the outcome is either a skip with its reason or a pass to the work stage."""
    REQUIRES = ()     # the seat it is handed

    def __init__(self, quest: QuestOperations) -> None:
        self.quest = quest

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]:
        clock = (lambda: datetime.fromisoformat(now.replace("Z", "+00:00"))) if now else datetime.now
        result = AutopilotPass(self.quest, team_id=getattr(self.quest, "team_id", "") or None, now=clock).run({"text": "pass", "goal_id": quest_id})
        if result.skipped:
            return {"outcome": "skipped", "text": result.skipped[0]["reason"]}
        return {"outcome": "ran", "text": ""}


@solution("think-critically", kind="event", entry="hold",
          example={"kind": "deep", "has_metric": True, "changed": True, "before": 0.4, "after": 0.6})
@typing.runtime_checkable
class MetricHolding(typing.Protocol):
    """Holds a request to the metric that serves it: the rows of its reading before and after that the code kept, whether a change ended with none."""

    def hold(self, case: Mapping[str, Any]) -> Mapping[str, Any]: ...


@implementation("think-critically", "deep_runs_only")
class DeepRunsOnly:
    """Around every deep goal the slice is found, the metric is read before and after by the runner's meters and the rows go to the ledger;
    a changed run with nothing on record is marked missing. ``hold`` returns that record for one request; the other methods hand a question
    to the member that owns it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, iteration: Lazy[GoalIteration], specs: Lazy[SpecSelecting], judges: Lazy[DecisionJudging], guards: Lazy[ReplyGuarding],
                 intents: Lazy[IntentGrounding], stalls: Lazy[StallDetecting], drafts: Lazy[MetricDrafting], gates: Lazy[PassGating]) -> None:
        self.iteration, self.specs, self.judges, self.guards = iteration, specs, judges, guards
        self.intents, self.stalls, self.drafts, self.gates = intents, stalls, drafts, gates

    def hold(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        return hold_case(case)

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        return self.iteration.slice_for(request, working_dir)

    def select(self, task: str, index: Dict[str, Any]) -> Any:
        return self.specs(task, index)

    def reach_verdict(self, raw: str) -> Optional[Dict[str, Any]]:
        return self.judges.reach_verdict(raw)

    def links(self, text: str) -> List[Any]:
        return self.guards.links(text)

    def frame(self, quest_id: str) -> Optional[Mapping[str, Any]]:
        return self.intents.frame(quest_id)

    def review(self, elapsed: str, idle: str, tail: str) -> Mapping[str, Any]:
        return self.stalls.review(elapsed, idle, tail)

    def draft(self, task_text: str, card: Mapping[str, Any], context_view: str = "") -> Any:
        return self.drafts.draft(task_text, card, context_view)

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]:
        return self.gates.gate(quest_id, now)
