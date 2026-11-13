"""Names a request holds (QAR, AHOHS, ESOI, an acronym in a file beside the work) are placed before the turn is planned; a name nothing placed leaves the goal open and
the terminal never calls such an answer met."""
from types import SimpleNamespace

from quest_ai_runner.the_consciousness.terminal.textual_ui_outcome import OUTCOME_UNMET, closing_text, turn_outcome
from quest_ai_runner.the_self.role_understanding import terms


def test_qar_is_placed_from_the_glossary_without_a_search(tmp_path):
    found = terms.ground("In what ways is QAR more capable than Claude Code?", [str(tmp_path)])
    assert found[0]["grounded"] and found[0]["expansion"] == "Quest AI Runner" and found[0]["basis"] == "glossary"
    assert terms.open_names(found) == []


def test_the_hubs_own_names_are_placed_too(tmp_path):
    found = {t["term"]: t for t in terms.ground("What do AHOHS and ESOI stand for?", [str(tmp_path)])}
    assert found["AHOHS"]["expansion"] == "AI Hub of Hubs Structure" and found["ESOI"]["expansion"] == "Evolvable Structure that Optimizes Intent"


def test_a_name_defined_in_a_file_beside_the_work_is_placed_and_an_undefined_one_is_not(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("Zonal Queue Exchange (ZQE) is the broker that moves jobs between zones.\n", encoding="utf-8")
    found = {t["term"]: t for t in terms.ground("Does ZQE beat WVTQ?", [str(tmp_path)])}
    assert found["ZQE"]["grounded"] and found["ZQE"]["basis"] == "files"
    assert terms.open_names(list(found.values())) == ["WVTQ"]
    assert "Do not guess" in terms.view_block(list(found.values()))


def test_an_answer_the_goal_check_found_short_is_not_shown_as_met():
    short = SimpleNamespace(kind="answer", goal_verdict={"met": False, "names_unplaced": ["WVTQ"]}, deep_results=[], decision_id=None)
    assert turn_outcome(short) == OUTCOME_UNMET and closing_text(OUTCOME_UNMET)[0] == "~ Goal not met"
    for verdict in (None, {"met": True}, {}):
        assert turn_outcome(SimpleNamespace(kind="answer", goal_verdict=verdict, deep_results=[], decision_id=None)) is None
