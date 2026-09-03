"""A queued task's brief must never be read as the user vetoing action.

The no-action gate in ``Orchestrator.run`` exists so a human can say "don't open a task, just
answer me here" and be obeyed. It was being applied to the text the executor passes when it works
a QUEUED TASK -- which is not a conversational turn at all, but a machine-composed brief: goal
descriptions, carried-over notes, lines from a month review.

Real briefs are full of ordinary status lines that read as a veto out of context. The phrase that
actually did it was the bare hold phrase "not yet" -- in "not yet booked", "has not yet marked
acted on", and a prior run's own "goal not yet met", none of which the human typed. When the gate
tripped, a planner
``action="deep"`` degraded to "answer": the run lost its tools, never touched the filesystem, and
then reported as though it had looked and found nothing. A silent false-completion, visible only
as one INFO line in the journal.

Offline: the real Orchestrator/TaskExecutor with a capturing deep runner, mirroring
``test_working_dir_override``. The deep runner being called at all IS the assertion -- a vetoed
turn never reaches it.
"""
from quest_ai_runner.core.orchestrator import message_forbids_new_task

from .test_working_dir_override import CapturingDeepRunner, _brain
from .test_runner import MockQuestClient
from quest_ai_runner.runner.executor import TaskExecutor

# Lines lifted verbatim in shape from the real autopilot brief that degraded a live run.
BRIEF_WITH_HOLD_PHRASES = (
    "Act as Bailey.\n\n"
    "Goal: Write literature review Domain 4.\n"
    "5. First chair meeting offered 8/22 -- not yet booked (weekly check-ins are separate).\n"
    "Insights the person captured since 2026-08-25 and has not yet marked acted on:\n"
    "Task [failed] Daily brief -> goal not yet met: the worker's output was truncated."
)


def test_the_heuristic_really_does_trip_on_a_real_brief():
    """Pin the premise: this is not a hypothetical. If this ever goes False the guard below is
    still correct, but it has stopped testing the thing that actually broke."""
    assert message_forbids_new_task(BRIEF_WITH_HOLD_PHRASES) is True


def test_a_task_brief_containing_hold_phrases_still_runs_deep():
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(deep))
    ex.execute({"id": "t1", "text": BRIEF_WITH_HOLD_PHRASES, "goal_id": "g1"})
    assert deep.calls, (
        "the deep run was skipped: the queued task's own brief was read as the user forbidding "
        "action, so the run degraded to a text-only answer with no filesystem"
    )


def test_a_human_veto_in_a_live_chat_turn_is_still_honored():
    """The other direction, which is the whole point of the gate: a real typed veto still stops a
    deep run. This is a LIVE turn straight into the orchestrator, not a queued task."""
    deep = CapturingDeepRunner()
    brain = _brain(deep)
    brain.run("don't create a task, just answer me here: what do you think of the plan?")
    assert deep.calls == []
