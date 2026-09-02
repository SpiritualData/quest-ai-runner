"""``related_goal_id`` lets a task scope its goal context to ONE goal inside a quest.

Background: a task document's ``goal_id`` field actually holds the QUEST's own id whenever the
task was created against a quest (a historical naming accident this repo does not rename -- see
``TaskExecutor._build_context_view``'s docstring). That leaves no way for a task to say "this run
is about goal X within this quest" and have the goal-context fetch pick it up: the existing fetch
only ever fires when a task ALSO carries a genuine ``quest_id`` key, which a quest-scoped task
never does.

``related_goal_id`` closes that gap: when present, it is the real goal to fetch and ``goal_id``
supplies the quest id to fetch it with. The two cases this covers:

  (a) a task with ``related_goal_id`` set gets goal context fetched, resolved against the right
      quest id
  (b) a task without it (the overwhelming majority today, since no backend sends it yet) behaves
      exactly as before -- this must stay a pure regression case
"""
from __future__ import annotations

from quest_ai_runner.runner.executor import TaskExecutor


class GoalClient:
    """The slice of QuestClient the goal-context fetch touches: ``get_goal`` only.

    No ``get_quest``/notes/history methods on purpose: ``_build_context_view`` checks each with
    ``getattr(..., None)`` before calling it, so a client that only offers ``get_goal`` exercises
    the goal fetch in isolation without those other sections needing to be stubbed too.
    """

    def __init__(self, goals: dict):
        self._goals = goals
        self.calls: list[tuple[str, str | None]] = []

    def get_goal(self, goal_id, *, quest_id=None):
        self.calls.append((goal_id, quest_id))
        return dict(self._goals.get(goal_id, {}))


def _executor(client) -> TaskExecutor:
    """A TaskExecutor with no orchestrator: ``_build_context_view`` never touches one."""
    return TaskExecutor(client, None)


def test_related_goal_id_fetches_the_specific_goal_using_goal_id_as_the_quest_id():
    """(a) related_goal_id present: the real goal is fetched, scoped to the resolved quest id.

    ``goal_id="quest_1"`` here plays the role it actually plays on a real quest-scoped task: the
    quest's own id, per the misnomer. ``related_goal_id="goal_9"`` is the specific goal.
    """
    client = GoalClient({"goal_9": {"name": "Draft the outline", "description": "First pass"}})

    view = _executor(client)._build_context_view(
        "quest_1", None, related_goal_id="goal_9")

    assert client.calls == [("goal_9", "quest_1")]
    assert "Goal: Draft the outline" in view
    assert "Goal description: First pass" in view


def test_related_goal_id_absent_is_byte_for_byte_the_prior_behavior():
    """(b) regression: no related_goal_id -> the single-id path runs exactly as it did before."""
    client = GoalClient({"goal_1": {"name": "Ship the draft"}})

    before = _executor(client)._build_context_view("goal_1", "quest_1")
    client.calls.clear()
    after = _executor(client)._build_context_view("goal_1", "quest_1", related_goal_id=None)

    assert before == after
    # Exactly the old call shape: goal_id first positional arg IS the goal id fetched, quest_id
    # (the real quest id param) is what it's scoped by -- untouched by the new parameter.
    assert client.calls == [("goal_1", "quest_1")]
    assert "Goal: Ship the draft" in after


def test_related_goal_id_without_a_resolvable_quest_id_fetches_nothing():
    """If the misnomer field (goal_id) is itself empty, there is no quest id to resolve against,
    so the fetch is correctly skipped rather than guessing -- same "no fetch" outcome as today's
    ``if goal_id and quest_id`` guard produces when either id is missing."""
    client = GoalClient({"goal_9": {"name": "Draft the outline"}})

    view = _executor(client)._build_context_view(None, None, related_goal_id="goal_9")

    assert client.calls == []
    assert "Draft the outline" not in view
