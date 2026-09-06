"""``config.resolve_rep_sync_resolver`` — ``RunnerConfig.personas`` producing a
``rep_sync_resolver``, and an explicit ``rep_sync_resolver`` always winning over it.

Two independent consumers each hand-wrote the same persona-resolution machinery before
``runner.personas.PersonaResolverConfig`` existed (a registry of ids -> skill folders, plus a
policy for picking one from a task). This is the config.py-level seam that lets a THIRD consumer —
or either of the first two, once migrated — get the same behavior from a few lines of
``RunnerConfig(personas=...)`` instead of writing a resolver by hand. ``Poller.__init__`` calls
this at construction time and writes the result back onto ``config.rep_sync_resolver`` (see
``runner/poller.py``); these tests exercise the resolution function directly, offline.
"""
from __future__ import annotations

from quest_ai_runner.config import RunnerConfig, resolve_rep_sync_resolver
from quest_ai_runner.runner.personas import PersonaResolverConfig


def test_neither_set_stays_none():
    cfg = RunnerConfig()
    assert resolve_rep_sync_resolver(cfg) is None


def test_personas_alone_composes_a_resolver(tmp_path):
    registry_file = tmp_path / "registry.json"
    registry_file.write_text('{"user_1": "alex"}')
    cfg = RunnerConfig(personas=PersonaResolverConfig(
        skills_root=str(tmp_path), registry_file=str(registry_file)))
    resolver = resolve_rep_sync_resolver(cfg)
    assert callable(resolver)
    result = resolver({"assignee_user_id": "user_1"})
    assert result == ("user_1", str(tmp_path / "alex"))


def test_personas_resolver_returns_none_for_an_unroutable_task(tmp_path):
    cfg = RunnerConfig(personas=PersonaResolverConfig(skills_root=str(tmp_path)))
    resolver = resolve_rep_sync_resolver(cfg)
    assert resolver({"text": "no assignment fields at all"}) is None


def test_explicit_rep_sync_resolver_wins_even_when_personas_is_also_set(tmp_path):
    def my_resolver(task):
        return ("explicit_id", "explicit_dir")

    cfg = RunnerConfig(
        rep_sync_resolver=my_resolver,
        personas=PersonaResolverConfig(skills_root=str(tmp_path)),
    )
    resolved = resolve_rep_sync_resolver(cfg)
    assert resolved is my_resolver


def test_explicit_rep_sync_resolver_alone_is_returned_unchanged():
    def my_resolver(task):
        return None

    cfg = RunnerConfig(rep_sync_resolver=my_resolver)
    assert resolve_rep_sync_resolver(cfg) is my_resolver


def test_does_not_mutate_cfg_itself():
    """Matches resolve_deep_runner's contract: the CALLER (Poller) writes the result back."""
    cfg = RunnerConfig(personas=PersonaResolverConfig(skills_root="/tmp"))
    resolve_rep_sync_resolver(cfg)
    assert cfg.rep_sync_resolver is None


def test_poller_writes_the_resolved_callable_back_onto_config(monkeypatch, tmp_path):
    from tests.conftest import StubProvider, StubRetrieval
    from quest_ai_runner.runner.poller import Poller

    registry_file = tmp_path / "registry.json"
    registry_file.write_text('{"user_1": "alex"}')
    cfg = RunnerConfig(
        quest_base_url="https://quest.example",
        quest_api_key="qsk_test",
        team_id="team_1",
        retrieval=StubRetrieval({}),
        model_provider=StubProvider([]),
        personas=PersonaResolverConfig(skills_root=str(tmp_path), registry_file=str(registry_file)),
    )
    assert cfg.rep_sync_resolver is None
    poller = Poller(cfg, state_path=str(tmp_path / "state.json"))
    assert poller.cfg.rep_sync_resolver is not None
    assert poller.cfg.rep_sync_resolver({"assignee_user_id": "user_1"}) == \
        ("user_1", str(tmp_path / "alex"))
