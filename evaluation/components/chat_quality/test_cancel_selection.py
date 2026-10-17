"""Offline unit tests for the approval-card cleanup's SELECTION filters: which open
decision-requests are eligible to cancel, and which must never be touched.

Lives beside the harness, not under ``tests/`` (``pyproject.toml`` scopes ``testpaths`` to
``tests/``, which the public library's offline suite runs by default): importing ``world`` pulls
in ``devclient``, which refuses to load unless pointed at this machine's dev lane .env, the same
constraint every other module in this DEV ONLY harness already carries. Run explicitly:

    .venv/bin/python3 -m pytest evaluation/qualitative/test_cancel_selection.py -q

No network calls: every function under test here is a pure filter over fabricated dicts.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import world as W  # noqa: E402
from cancel_own_asks import cancellable  # noqa: E402

FIELD_EDIT = {"decision_id": "teamdec_a1", "executable": {"kind": "field_edit"}, "quest_id": "q1"}
QUEST_COMMAND = {"decision_id": "teamdec_a2", "executable": {"kind": "quest_command"}, "quest_id": "q1"}
QUEST_CREATION = {"decision_id": "teamdec_a3", "executable": {"kind": "machine_quest_creation"},
                  "quest_id": None}
NO_EXECUTABLE = {"decision_id": "teamdec_a4", "quest_id": "q1"}  # e.g. a routing_choice decision


# ---------------------------------------------------------------------------------------------
# world.cancellable_decision_ids
# ---------------------------------------------------------------------------------------------

def test_field_edit_and_quest_command_are_eligible():
    ids = W.cancellable_decision_ids([FIELD_EDIT, QUEST_COMMAND], pending_asks=set())
    assert ids == ["teamdec_a1", "teamdec_a2"]


def test_quest_creation_kind_is_never_eligible():
    ids = W.cancellable_decision_ids([QUEST_CREATION], pending_asks=set())
    assert ids == []


def test_quest_creation_excluded_even_if_also_listed_as_pending():
    # Belt and braces: excluded by kind alone, whether or not it is also in world_asks.json.
    ids = W.cancellable_decision_ids([QUEST_CREATION], pending_asks={"teamdec_a3"})
    assert ids == []


def test_pending_world_ask_is_never_eligible_even_with_a_different_kind():
    # A still-open world quest-creation ask has no executable yet in practice, but the pending-ask
    # exclusion must hold even if one somehow carried a different kind (defence in depth).
    row = {"decision_id": "teamdec_a1", "executable": {"kind": "field_edit"}, "quest_id": "q1"}
    ids = W.cancellable_decision_ids([row], pending_asks={"teamdec_a1"})
    assert ids == []


def test_row_without_a_decision_id_is_skipped():
    ids = W.cancellable_decision_ids([NO_EXECUTABLE, {"executable": {"kind": "field_edit"}}],
                                     pending_asks=set())
    assert ids == ["teamdec_a4"]


def test_empty_input_is_empty_output():
    assert W.cancellable_decision_ids([], pending_asks=set()) == []


# ---------------------------------------------------------------------------------------------
# world.tagged_quest_decision_ids
# ---------------------------------------------------------------------------------------------

def test_only_rows_on_tagged_quests_are_kept():
    tagged = {"q_eval": True, "q_other": False}
    rows = [{"decision_id": "d1", "quest_id": "q_eval"},
           {"decision_id": "d2", "quest_id": "q_other"}]
    assert W.tagged_quest_decision_ids(rows, tagged.get) == ["d1"]


def test_row_with_no_quest_id_is_never_kept():
    rows = [{"decision_id": "d1", "quest_id": None}, {"decision_id": "d2"}]
    # is_tagged_quest would raise on None/missing if ever called; it must not be reached.
    assert W.tagged_quest_decision_ids(rows, lambda qid: (_ for _ in ()).throw(
        AssertionError("should not be called"))) == []


def test_unknown_quest_is_not_guessed_into_the_sweep():
    # is_tagged_quest returning False for a quest it could not confirm (deleted, never tagged)
    # must exclude the row, not include it.
    rows = [{"decision_id": "d1", "quest_id": "gone"}]
    assert W.tagged_quest_decision_ids(rows, lambda qid: False) == []


# ---------------------------------------------------------------------------------------------
# cancel_own_asks.cancellable (the backend-side script's own, independent safety gate)
# ---------------------------------------------------------------------------------------------

SELF_AUTHORED_OPEN = {"status": "open", "assigned_to_user_id": "u1", "created_by": "u1",
                      "executable": {"kind": "field_edit"}}


def test_missing_row_refused():
    assert cancellable(None) == "not found"


def test_already_resolved_row_refused():
    row = dict(SELF_AUTHORED_OPEN, status="resolved")
    assert "not open" in cancellable(row)


def test_quest_creation_kind_refused_even_if_passed_in():
    row = dict(SELF_AUTHORED_OPEN, executable={"kind": "machine_quest_creation"})
    assert "machine_quest_creation" in cancellable(row)


def test_decision_assigned_to_someone_else_refused():
    row = dict(SELF_AUTHORED_OPEN, created_by="u2")
    assert "self-authored" in cancellable(row)


def test_decision_with_no_assignee_refused():
    row = dict(SELF_AUTHORED_OPEN, assigned_to_user_id=None, created_by=None)
    assert "self-authored" in cancellable(row)


def test_valid_self_authored_open_field_edit_is_cancellable():
    assert cancellable(SELF_AUTHORED_OPEN) is None


def test_valid_self_authored_open_quest_command_is_cancellable():
    row = dict(SELF_AUTHORED_OPEN, executable={"kind": "quest_command"})
    assert cancellable(row) is None
