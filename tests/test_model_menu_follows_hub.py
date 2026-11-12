"""The terminal's /model and menu follow the hub's tiers and offer the models the hub serves; no tier or model name is written in QAR."""
from types import SimpleNamespace

from hubofhubs.api import role_tiers

from quest_ai_runner.the_consciousness.terminal.interactive_session_commands import CommandsMixin


class Console:
    def __init__(self):
        self.lines = []

    def dim(self, text):
        self.lines.append(text)


class Registry:
    def top_models(self):
        return {"fast": "m-fast", "balanced": "m-bal", "quality": "m-q", "best": "m-best"}

    def served_models(self):
        return ["m-fast", "m-bal", "m-q", "m-best", "m-extra"]

    def explicit_model(self, name):
        return next((m for m in self.served_models() if m == (name or "").strip().lower()), None)


class Session(CommandsMixin):
    def __init__(self):
        self._console, self._model_hint, self._orch = Console(), None, SimpleNamespace(registry=Registry())

    def _persist_session_state(self):
        pass


def test_menu_lists_the_hubs_tiers_then_its_models():
    s = Session()
    s._build_model_tiers_menu()
    names = [n for n, _ in s._model_tiers]
    assert names == ["auto", *role_tiers.TIERS, "m-extra"] or names[:1 + len(role_tiers.TIERS)] == ["auto", *role_tiers.TIERS]
    assert "m-extra" in names and "m-best" in names


def test_menu_follows_a_changed_tiers_tuple(monkeypatch):
    monkeypatch.setattr(role_tiers, "TIERS", ("fast", "balanced"))
    s = Session()
    s._build_model_tiers_menu()
    assert [n for n, _ in s._model_tiers][:3] == ["auto", "fast", "balanced"] and "quality" not in [n for n, _ in s._model_tiers]


def test_model_command_takes_a_tier_or_a_served_model_and_refuses_others():
    s = Session()
    s._cmd_model("best")
    assert s._model_hint == "best"
    s._cmd_model("m-extra")
    assert s._model_hint == "m-extra"
    s._cmd_model("nonsense")
    assert s._model_hint == "m-extra"
