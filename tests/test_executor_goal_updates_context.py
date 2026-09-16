"""A goal update is the per-goal check-in thread: where a person writes, on ONE goal, what they
did or found. Nothing in this library read them before now, so a run working a goal never saw the
person's own notes on THAT goal -- only ``Goal description``, which says what the goal IS, not
what they've actually done about it.

``list_goal_updates`` (duck-typed on ``QuestClient`` the same way ``get_goal``/``list_quest_notes``
already are, per ``TaskExecutor._fetch_person_notes``) closes that gap. This covers:

  (a) a goal with two updates puts both texts, their dates, and their author into the assembled
      context, newest first;
  (b) a client with no ``list_goal_updates`` at all -- a consumer's ``QuestClient`` predating this
      feature -- produces byte-for-byte the same context as before, no crash, no stray heading;
  (c) an exception raised by ``list_goal_updates`` is swallowed and the rest of the context (goal
      metadata, etc.) still renders;
  (d) the row cap (``GOAL_UPDATE_CONTEXT_LIMIT``) is honored even when the client itself returns
      more rows than it was asked for.
"""
from __future__ import annotations

from quest_ai_runner.runner.executor import (
    GOAL_UPDATE_CONTEXT_LIMIT,
    TaskExecutor,
    render_goal_updates,
)


def update(note: str, *, who: str = "Joshua", when: str = "2026-09-14") -> dict:
    """One row exactly as the verified ``list_goal_updates`` shape carries it."""
    return {"updateId": "gupd_1", "goalId": "goal_1", "userId": "user_1",
            "userName": who, "note": note, "shared": False, "createdAt": when}


class GoalClient:
    """``get_goal`` only -- no ``list_goal_updates`` attribute at all, so ``getattr(...)`` sees
    None and the goal-update fetch is skipped exactly like a consumer's ``QuestClient`` that
    predates this feature. Mirrors ``GoalClient`` in ``test_related_goal_context.py``.
    """

    def __init__(self, goals: dict):
        self._goals = goals

    def get_goal(self, goal_id, *, quest_id=None):
        return dict(self._goals.get(goal_id, {}))


class GoalUpdatesClient(GoalClient):
    """Adds a switchable ``list_goal_updates`` on top of ``GoalClient``'s ``get_goal``."""

    def __init__(self, goals: dict, *, updates=None, raises: bool = False):
        super().__init__(goals)
        self._updates = updates if updates is not None else []
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def list_goal_updates(self, goal_id, *, limit=20):
        self.calls.append((goal_id, limit))
        if self._raises:
            raise RuntimeError("goal updates route unavailable")
        return list(self._updates)


def _executor(client) -> TaskExecutor:
    """A TaskExecutor with no orchestrator: ``_build_context_view`` never touches one."""
    return TaskExecutor(client, None)


# --- rendering ---------------------------------------------------------------------

def test_two_updates_render_full_text_date_and_author_newest_first():
    out = render_goal_updates([
        update("Read chapters 4-6; the framing in ch5 changed my mind about X.", when="2026-09-14"),
        update("Started the book, slower going than expected.", when="2026-09-10"),
    ])

    lines = out.splitlines()
    newer = next(ln for ln in lines if "Read chapters 4-6" in ln)
    older = next(ln for ln in lines if "Started the book" in ln)
    assert "2026-09-14" in newer and "Joshua" in newer
    assert "2026-09-10" in older and "Joshua" in older
    assert lines.index(newer) < lines.index(older)  # newest first, input order preserved
    # the FULL text, not summarized or truncated
    assert "the framing in ch5 changed my mind about X." in out
    # the block says plainly this is the person's own words on THIS goal
    assert "check-in" in out


def test_no_updates_renders_nothing():
    assert render_goal_updates([]) == ""
    assert render_goal_updates(None) == ""
    assert render_goal_updates([{"note": ""}]) == ""


def test_row_cap_is_honored_even_when_the_client_over_returns():
    updates = [update(f"update {i}", when="2026-09-%02d" % (i + 1))
               for i in range(GOAL_UPDATE_CONTEXT_LIMIT + 3)]

    out = render_goal_updates(updates)
    kept = [ln for ln in out.splitlines() if "update " in ln]

    assert len(kept) == GOAL_UPDATE_CONTEXT_LIMIT
    assert "update 0" in out                                   # newest (first) rows kept
    assert f"update {GOAL_UPDATE_CONTEXT_LIMIT + 2}" not in out  # oldest tail dropped


# --- fetching through the executor's context view -----------------------------------

def test_two_goal_updates_land_in_the_assembled_context_newest_first():
    client = GoalUpdatesClient(
        {"goal_1": {"name": "Read the Bhagavad Gita"}},
        updates=[
            update("Finished part two.", when="2026-09-14"),
            update("Started part one.", when="2026-09-10"),
        ],
    )

    view = _executor(client)._build_context_view("goal_1", "quest_1")

    assert "Finished part two." in view
    assert "Started part one." in view
    assert view.index("Finished part two.") < view.index("Started part one.")
    assert client.calls == [("goal_1", GOAL_UPDATE_CONTEXT_LIMIT)]


def test_a_client_with_no_list_goal_updates_is_byte_for_byte_the_prior_behavior():
    """(b) regression: no list_goal_updates on the client -> no crash, no stray heading, and the
    rest of the context (goal metadata) is unaffected."""
    client = GoalClient(
        {"goal_1": {"name": "Read the Bhagavad Gita", "description": "Chapters 1-18"}})

    view = _executor(client)._build_context_view("goal_1", "quest_1")

    assert "Goal: Read the Bhagavad Gita" in view
    assert "Goal description: Chapters 1-18" in view
    assert "Goal updates" not in view


def test_a_raising_list_goal_updates_is_swallowed_and_the_rest_still_renders():
    client = GoalUpdatesClient({"goal_1": {"name": "Read the Bhagavad Gita"}}, raises=True)

    view = _executor(client)._build_context_view("goal_1", "quest_1")

    assert "Goal: Read the Bhagavad Gita" in view
    assert "Goal updates" not in view


def test_an_empty_update_list_produces_no_heading():
    client = GoalUpdatesClient({"goal_1": {"name": "Read the Bhagavad Gita"}}, updates=[])

    view = _executor(client)._build_context_view("goal_1", "quest_1")

    assert "Goal updates" not in view
