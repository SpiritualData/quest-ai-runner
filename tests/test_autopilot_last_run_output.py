"""The previous run's own output, carried forward with no period window at all.

The regression this pins: ``_previous_period_summary`` (see its own docstring) only ever describes
tasks that finished inside the previous CALENDAR period, which is the wrong question for a
recurring pass asking "what did I just do". For a week/month/quarter/year-scoped quest, the
previous period is last week/month/quarter/year, so a pass that ran on this quest YESTERDAY sits
inside the CURRENT period and never shows up there -- a daily pass on a week-scoped quest could not
see its own run from the day before. An unscoped quest fares worse: it gets no previous-period view
at all (``_previous_period_summary`` returns ``None`` outright). ``select_last_run_output`` and
``render_last_run_output`` answer a different, simpler question -- what is the most recent thing
this quest's own runner output actually said, independent of any period boundary -- which is
answerable for every quest, scoped or not.

Driven with plain dicts, offline. Two of the tests below instantiate ``AutopilotPass`` against the
``tests/test_autopilot.py`` fake purely to call its private helpers directly with hand-built task
lists (``_previous_period_summary``, ``_last_run_output``), the same pattern
``tests/test_autopilot.py`` already uses for ``_eligible_quests``/``_handle_proposal``/
``_maybe_create_goal``; no network and no full pass run needed.
"""
from quest_ai_runner.runner.autopilot import (
    AUTOPILOT_PASS_KIND,
    AUTOPILOT_WORK_KIND,
    LAST_RUN_OUTPUT_MAX_CHARS,
    AutopilotPass,
    compose_batch_text,
    current_scope_label,
    render_last_run_output,
    select_last_run_output,
)

from .test_autopilot import NOW, FakeAutopilotClient, _goal, _goals_payload, _now


def _passer():
    # ``_previous_period_summary``/``_last_run_output`` touch no client method directly (the task
    # list is handed to them, not fetched by them), so an empty fake is enough to construct one.
    return AutopilotPass(FakeAutopilotClient(), now=_now)


# --- the exact regression: a week-scope quest's own previous period cannot see yesterday --------

def test_week_scope_quest_sees_yesterdays_run_even_though_previous_period_excludes_it():
    """NOW is 2026-07-12 (Sunday); the current ISO week runs Mon 2026-07-06 through today, so the
    PREVIOUS week is 2026-06-29 through 2026-07-06. A task worked yesterday (2026-07-11) sits
    inside the CURRENT week, not the previous one -- asserted directly below, so this test fails
    loudly if the window arithmetic ever changes to accidentally include it, which would make the
    regression untestable rather than fixed. ``select_last_run_output`` has no such window, so it
    still finds the same task, and it still reaches the composed batch text as a last-run block.
    """
    yesterday_task = {
        "id": "atask_y1", "task_kind": AUTOPILOT_WORK_KIND, "status": "done",
        "result": "Drafted the intro paragraph for the newsletter.",
        "worked_at": "2026-07-11T10:00:00Z",
    }
    goals_payload = _goals_payload(("week", "2026_W28", [_goal("w1", "Ship the newsletter")]))
    passer = _passer()

    previous = passer._previous_period_summary("q1", goals_payload, "week:2026_W28",
                                                [yesterday_task])
    assert previous is not None
    assert yesterday_task not in (previous.get("tasks") or []), (
        "the previous-period window is supposed to exclude yesterday for a week-scoped quest -- "
        "if this assertion fails, the regression this file exists to cover is no longer "
        "reproducible and the test above it needs rethinking")

    selected = select_last_run_output([yesterday_task])
    assert selected is yesterday_task
    text = compose_batch_text("Ship the newsletter", last_run=render_last_run_output(selected))
    assert "Drafted the intro paragraph for the newsletter." in text


def test_unscoped_quest_still_gets_a_last_run_block():
    """A quest with no current period group gets ``scope_label == "unscoped"``, and
    ``_previous_period_summary`` returns ``None`` for it outright -- the coarser gap this feature
    also closes, not just the week/month/etc. case above."""
    task = {
        "id": "atask_1", "task_kind": AUTOPILOT_PASS_KIND, "status": "done",
        "result": "Reviewed the outreach list and flagged three leads.",
        "worked_at": "2026-07-11T10:00:00Z",
    }
    goals_payload = _goals_payload()  # no period groups at all
    scope_label = current_scope_label(goals_payload, NOW)
    assert scope_label == "unscoped"
    passer = _passer()

    previous = passer._previous_period_summary("q1", goals_payload, scope_label, [task])
    assert previous is None

    last_run_text = passer._last_run_output([task], previous)
    assert last_run_text is not None
    assert "Reviewed the outreach list and flagged three leads." in last_run_text


# --- select_last_run_output: the selection rules, in isolation ----------------------------------

def test_pass_row_preferred_over_its_own_more_recent_child_work_row():
    """quest-backend's autopilot_rollup writes a finished work task's output back onto its parent
    pass row, so once that has happened the pass row is the more complete of the two (it can carry
    several children's output, not just the one that finished most recently). The work row here is
    the more RECENT candidate by timestamp; the pass row must still win."""
    pass_row = {
        "id": "atask_pass", "task_kind": AUTOPILOT_PASS_KIND, "status": "done",
        "result": ("## Draft newsletter\n\nDrafted the intro.\n\n"
                  "## Outreach review\n\nFlagged three leads."),
        "worked_at": "2026-07-11T09:00:00Z",
    }
    work_row = {
        "id": "atask_work", "task_kind": AUTOPILOT_WORK_KIND, "status": "done",
        "parent_task_id": "atask_pass", "result": "Flagged three leads.",
        "worked_at": "2026-07-11T10:00:00Z",  # more recent than the pass row
    }
    assert select_last_run_output([pass_row, work_row]) is pass_row


def test_goal_proposal_never_selected():
    """A proposal's TEXT (not its result) carries ``PROPOSAL_TEXT_PREFIX``, mirroring how the rest
    of this module recognizes one. Given a non-empty result too (unusual, but exercised here so the
    skip is provably about the proposal check and not the separate empty-result check), it must
    still lose to an ordinary finished task."""
    proposal = {
        "id": "atask_p1", "task_kind": AUTOPILOT_WORK_KIND, "status": "suggested",
        "text": "Proposed goal: Ship v2\n\nA description of the proposed goal.",
        "result": "This would otherwise look selectable.",
        "worked_at": "2026-07-12T08:00:00Z",
    }
    work = {
        "id": "atask_w2", "task_kind": AUTOPILOT_WORK_KIND, "status": "done",
        "result": "Wrote the draft.", "worked_at": "2026-07-10T08:00:00Z",
    }
    assert select_last_run_output([proposal, work]) is work


def test_task_with_empty_or_none_result_never_selected():
    empty = {"id": "a1", "task_kind": AUTOPILOT_WORK_KIND, "status": "done", "result": "   ",
            "worked_at": "2026-07-12T08:00:00Z"}
    none_result = {"id": "a2", "task_kind": AUTOPILOT_WORK_KIND, "status": "done", "result": None,
                  "worked_at": "2026-07-11T08:00:00Z"}
    assert select_last_run_output([empty, none_result]) is None


# --- render_last_run_output: truncation --------------------------------------------------------

def test_truncation_at_the_budget_appends_a_visible_marker():
    long_result = "x" * (LAST_RUN_OUTPUT_MAX_CHARS + 500)
    task = {"status": "done", "result": long_result, "worked_at": "2026-07-11T08:00:00Z"}
    text = render_last_run_output(task)
    assert long_result not in text          # actually cut, not appended in full
    assert "x" * LAST_RUN_OUTPUT_MAX_CHARS in text   # kept up to the budget
    assert "[... cut here" in text           # a visible marker, not a silent truncation


# --- compose_batch_text: the byte-identical guarantee, and de-duplication -----------------------

def test_compose_batch_text_with_last_run_none_is_byte_identical_to_omitted():
    assert (compose_batch_text("Ship it", last_run=None) == compose_batch_text("Ship it"))


def test_deduplication_drops_the_chosen_tasks_stub_from_the_previous_block():
    """The same task can be both "the most recent thing this quest ever produced" (picked by
    ``select_last_run_output``) and a row already sitting in ``previous["tasks"]`` (put there by
    ``_previous_period_summary``, since the previous period's most recent finished task is often
    also the quest's most recent finished task overall). Left alone it would print twice: once here
    in full, once more as its own 280-character stub a few lines later. ``_last_run_output`` drops
    it from ``previous["tasks"]`` when it hands back the rendered block."""
    task = {
        "id": "atask_dup", "task_kind": AUTOPILOT_WORK_KIND, "status": "done",
        "title": "Weekly digest", "result": "Sent the weekly digest to twelve subscribers.",
        "worked_at": "2026-07-05T08:00:00Z",
    }
    previous = {"period": "week:2026_W27", "tasks": [dict(task)]}
    passer = _passer()

    last_run_text = passer._last_run_output([task], previous)
    assert last_run_text is not None
    assert previous["tasks"] == []   # the stub-carrying row was dropped in place

    composed = compose_batch_text("Ship it", previous=previous, last_run=last_run_text)
    # Exactly once: the full text via the last-run block, never again as a truncated stub.
    assert composed.count("Sent the weekly digest to twelve subscribers.") == 1
    # And the last-run block reads BEFORE the previous-period block, per compose_batch_text's own
    # ordering rule (more specific and more recent material first).
    assert (composed.index("Sent the weekly digest to twelve subscribers.")
           < composed.index("What happened in the previous period"))
