"""Working inside a quest's folder means working on that quest, in that quest's voice.

Two small pieces that make an ATTENDED session line up with the autonomous one: resolving which
quest a filesystem path belongs to, and which persona that quest's roster puts on duty today.
Without them, opening a chat inside a quest gets a generic assistant while its autopilot runs as a
named character with that character's accumulated corrections, and the two read as unrelated
systems when they are meant to be one.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import personas_on_duty
from quest_ai_runner.runner.quest_folder_sync import quest_for_path

SUNDAY = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


# --- which quest am I standing in? ---------------------------------------------------------------

def test_a_path_inside_a_mapped_folder_resolves_to_its_quest(tmp_path):
    folder = tmp_path / "stories" / "phd"
    (folder / "method").mkdir(parents=True)
    mapping = {"quest_phd": str(folder)}
    assert quest_for_path(mapping, folder / "method") == ("quest_phd", str(folder))
    assert quest_for_path(mapping, folder) == ("quest_phd", str(folder))


def test_the_deepest_mapped_folder_wins(tmp_path):
    """Quest folders nest: a story folder holding a sub-project folder. The enclosing quest would
    otherwise shadow the specific one, which is exactly backwards."""
    outer = tmp_path / "stories"
    inner = outer / "phd"
    inner.mkdir(parents=True)
    mapping = {"quest_outer": str(outer), "quest_inner": str(inner)}
    assert quest_for_path(mapping, inner)[0] == "quest_inner"
    assert quest_for_path(mapping, outer)[0] == "quest_outer"


def test_a_path_outside_every_mapped_folder_resolves_to_nothing(tmp_path):
    (tmp_path / "mapped").mkdir()
    (tmp_path / "elsewhere").mkdir()
    mapping = {"q": str(tmp_path / "mapped")}
    assert quest_for_path(mapping, tmp_path / "elsewhere") is None


def test_an_empty_map_and_a_bad_path_are_answers_not_crashes(tmp_path):
    """This runs at chat startup, where a deleted cwd or an unreadable symlink must mean "no quest
    here" rather than a traceback in front of the user."""
    assert quest_for_path({}, tmp_path) is None
    assert quest_for_path({"q": str(tmp_path / "does-not-exist")}, tmp_path) is None
    assert quest_for_path({"q": ""}, tmp_path) is None


# --- who is on duty today? ------------------------------------------------------------------------

def test_only_todays_personas_are_on_duty():
    cfg = {"personas": [{"rep_id": "rep_bailey", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                        {"rep_id": "rep_batman", "days": ["Sat"]}]}
    assert personas_on_duty(cfg, MONDAY) == ["rep_bailey"]
    assert personas_on_duty(cfg, SUNDAY) == []          # nobody is rostered on Sunday


def test_a_day_restricted_persona_outranks_an_unrestricted_one():
    """Same precedence as resolve_persona: an explicit "Bailey on Mondays" beats a catch-all."""
    cfg = {"personas": [{"rep_id": "rep_anyone"},
                        {"rep_id": "rep_bailey", "days": ["Mon"]}]}
    assert personas_on_duty(cfg, MONDAY) == ["rep_bailey", "rep_anyone"]


def test_an_unrestricted_persona_is_on_duty_every_day():
    cfg = {"personas": [{"rep_id": "rep_anyone"}]}
    assert personas_on_duty(cfg, SUNDAY) == ["rep_anyone"]


def test_no_roster_means_nobody_and_duplicates_collapse():
    assert personas_on_duty({}, MONDAY) == []
    cfg = {"personas": [{"rep_id": "rep_a", "days": ["Mon"]}, {"rep_id": "rep_a"}]}
    assert personas_on_duty(cfg, MONDAY) == ["rep_a"]
