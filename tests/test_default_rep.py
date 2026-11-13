"""A session always has an AI rep: the one picked, else the quest's, else the team's default, else the hub's built-in General assistant. Offline, no model."""
from types import SimpleNamespace

from tests.test_opencode_terminal import Orchestrator, Session, owner_with
from quest_ai_runner.the_consciousness.terminal.interactive_session_persona import default_rep_name


def test_nothing_picked_gets_the_built_in_general_assistant(monkeypatch):
    monkeypatch.delenv("QAR_DEFAULT_REP", raising=False)
    name, how = default_rep_name(SimpleNamespace(), None)
    assert (name, how) == ("General assistant", "default")


def test_the_quests_rep_wins_over_the_teams_and_the_teams_over_the_default(monkeypatch):
    monkeypatch.setenv("QAR_DEFAULT_REP", "Team AI")
    assert default_rep_name(SimpleNamespace(), SimpleNamespace(rep_name="Quest AI")) == ("Quest AI", "quest")
    assert default_rep_name(SimpleNamespace(), SimpleNamespace(rep_name="")) == ("Team AI", "team")
    assert default_rep_name(SimpleNamespace(default_rep_name="Cfg AI"), None)[0] == "Team AI"        # the environment names the team's rep first


def test_the_header_names_the_rep_and_never_says_none_was_selected():
    session = Session(Orchestrator([], None))
    session._rep_name = "General assistant"
    assert owner_with(session).header()[0] == "AI: General assistant"
    session._rep_name = "Assistant"       # the old placeholder reads the same way
    line = owner_with(session).header()[0]
    assert line == "AI: General assistant" and "persona" not in line
