"""``RunnerConfig.from_file`` (TOML), ``apply_file_defaults`` (file-loses-to-env precedence), and
``cli._config_from_env``'s ``config_path`` / ``QAR_CONFIG_FILE`` wiring.

This library had no file-based config at all before this (no TOML, no YAML). The design: a file
supplies a BASELINE, an environment variable that sets the same field always wins — implemented
generically (``apply_file_defaults`` fills only fields still at ``RunnerConfig``'s own dataclass
default) rather than by threading a parallel check through every line of ``_config_from_env``.
Every failure mode is loud (``ConfigFileError``, a ``ValueError`` subclass) rather than silently
dropping a setting — see the module's own docstring in ``config.py`` for why that matters here.
"""
from __future__ import annotations

import textwrap

import pytest

from quest_ai_runner import cli
from quest_ai_runner.config import ConfigFileError, RunnerConfig, apply_file_defaults
from quest_ai_runner.runner.personas import PersonaResolverConfig


def _write(tmp_path, text: str, name: str = "qar.toml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


# --- from_file: happy path ---------------------------------------------------

def test_from_file_parses_scalar_fields(tmp_path):
    path = _write(tmp_path, """
        quest_base_url = "https://quest.example"
        team_id = "team_1"
        poll_interval_seconds = 123.0
        max_concurrent_tasks = 4
        autopilot_adopt_recurring = true
        channel_allowed_senders = ["alice", "bob"]
        model_fallback = { fast = "haiku", best = "opus" }
    """)
    cfg = RunnerConfig.from_file(path)
    assert cfg.quest_base_url == "https://quest.example"
    assert cfg.team_id == "team_1"
    assert cfg.poll_interval_seconds == 123.0
    assert cfg.max_concurrent_tasks == 4
    assert cfg.autopilot_adopt_recurring is True
    assert cfg.channel_allowed_senders == ["alice", "bob"]
    assert cfg.model_fallback == {"fast": "haiku", "best": "opus"}


def test_from_file_parses_personas_table(tmp_path):
    path = _write(tmp_path, """
        [personas]
        skills_root = "/srv/skills"
        llm_explicit_ask = true
        card_activation = true
        cards_dir = "/srv/skills/.cards"
        card_min_hits = 3
        auto_register = true
    """)
    cfg = RunnerConfig.from_file(path)
    assert isinstance(cfg.personas, PersonaResolverConfig)
    assert cfg.personas.skills_root == "/srv/skills"
    assert cfg.personas.llm_explicit_ask is True
    assert cfg.personas.card_activation is True
    assert cfg.personas.card_min_hits == 3
    assert cfg.personas.auto_register is True


def test_from_file_empty_table_is_a_valid_default_config(tmp_path):
    path = _write(tmp_path, "")
    cfg = RunnerConfig.from_file(path)
    assert cfg.team_id == ""
    assert cfg.personas is None


# --- from_file: loud failures ------------------------------------------------

def test_from_file_missing_file_raises_loudly():
    with pytest.raises(ConfigFileError, match="could not read"):
        RunnerConfig.from_file("/no/such/path/qar.toml")


def test_from_file_invalid_toml_raises_loudly(tmp_path):
    path = _write(tmp_path, "this is not [ valid toml")
    with pytest.raises(ConfigFileError, match="not valid TOML"):
        RunnerConfig.from_file(path)


def test_from_file_unknown_key_raises_loudly_and_names_it(tmp_path):
    path = _write(tmp_path, 'not_a_real_runnerconfig_field = 1\n')
    with pytest.raises(ConfigFileError, match="not_a_real_runnerconfig_field"):
        RunnerConfig.from_file(path)


def test_from_file_rejects_object_typed_fields_with_an_actionable_message(tmp_path):
    """``retrieval`` IS a real field name, but it holds a live adapter object — the error must
    say that, distinctly from "not a field at all", so the fix is obvious."""
    path = _write(tmp_path, 'retrieval = "not-an-adapter"\n')
    with pytest.raises(ConfigFileError, match="retrieval"):
        RunnerConfig.from_file(path)


def test_from_file_unknown_persona_key_raises_loudly(tmp_path):
    path = _write(tmp_path, """
        [personas]
        skills_root = "/srv/skills"
        not_a_real_persona_field = true
    """)
    with pytest.raises(ConfigFileError, match="not_a_real_persona_field"):
        RunnerConfig.from_file(path)


def test_from_file_personas_table_must_be_a_table(tmp_path):
    path = _write(tmp_path, 'personas = "not-a-table"\n')
    with pytest.raises(ConfigFileError, match="\\[personas\\] must be a table"):
        RunnerConfig.from_file(path)


# --- apply_file_defaults: the precedence rule --------------------------------

def test_apply_file_defaults_fills_untouched_fields():
    cfg = RunnerConfig(team_id="team_from_code")  # runner_label left at the class default (None)
    file_cfg = RunnerConfig(runner_label="label_from_file")
    apply_file_defaults(cfg, file_cfg)
    assert cfg.runner_label == "label_from_file"


def test_apply_file_defaults_never_overrides_an_already_set_field():
    cfg = RunnerConfig(team_id="team_from_code")
    file_cfg = RunnerConfig(team_id="team_from_file")
    apply_file_defaults(cfg, file_cfg)
    assert cfg.team_id == "team_from_code"


def test_apply_file_defaults_with_no_file_is_a_noop():
    cfg = RunnerConfig(team_id="team_from_code")
    result = apply_file_defaults(cfg, None)
    assert result is cfg
    assert cfg.team_id == "team_from_code"


# --- integration: cli._config_from_env(config_path=...) / QAR_CONFIG_FILE ---

def _base_env(monkeypatch):
    monkeypatch.setenv("QUEST_API_KEY", "qsk_test")


def test_config_from_env_env_var_wins_over_file(tmp_path, monkeypatch):
    path = _write(tmp_path, 'team_id = "team_from_file"\n')
    _base_env(monkeypatch)
    monkeypatch.setenv("QUEST_TEAM_ID", "team_from_env")
    cfg = cli._config_from_env(str(path))
    assert cfg.team_id == "team_from_env"


def test_config_from_env_file_fills_a_field_no_env_var_sets(tmp_path, monkeypatch):
    path = _write(tmp_path, 'runner_label = "label_from_file"\n')
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_RUNNER_LABEL", raising=False)
    cfg = cli._config_from_env(str(path))
    assert cfg.runner_label == "label_from_file"


def test_config_from_env_file_supplies_corpus_root_and_builds_a_retrieval_adapter(tmp_path, monkeypatch):
    """corpus_root is special-cased in _config_from_env (it drives FilesAdapter construction
    before the generic file-defaults layering even runs) — a file-only corpus must still work."""
    from quest_ai_runner.adapters import FilesAdapter

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    path = _write(tmp_path, f'corpus_root = "{corpus_dir}"\n')
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_CORPUS_ROOT", raising=False)
    cfg = cli._config_from_env(str(path))
    assert cfg.corpus_root == str(corpus_dir)
    # A FilesAdapter over the file-supplied corpus is wired in (possibly nested inside a
    # CompositeRetrievalAdapter alongside the conversation-search adapters, which are on by
    # default) -- the point is that a file-only corpus_root is not silently dropped.
    def _flatten(adapter):
        sub = getattr(adapter, "adapters", None)
        if sub is None:
            return [adapter]
        return [leaf for a in sub for leaf in _flatten(a)]

    leaves = _flatten(cfg.retrieval)
    assert any(isinstance(a, FilesAdapter) and a.root == corpus_dir.resolve() for a in leaves)


def test_config_from_env_reads_qar_config_file_env_var(tmp_path, monkeypatch):
    path = _write(tmp_path, 'runner_label = "from_qar_config_file_env"\n')
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_CONFIG_FILE", str(path))
    monkeypatch.delenv("QAR_RUNNER_LABEL", raising=False)
    cfg = cli._config_from_env()
    assert cfg.runner_label == "from_qar_config_file_env"


def test_config_from_env_explicit_config_path_beats_qar_config_file_env(tmp_path, monkeypatch):
    env_path = _write(tmp_path, 'runner_label = "from_env_var_file"\n', name="env.toml")
    explicit_path = _write(tmp_path, 'runner_label = "from_explicit_path"\n', name="explicit.toml")
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_CONFIG_FILE", str(env_path))
    monkeypatch.delenv("QAR_RUNNER_LABEL", raising=False)
    cfg = cli._config_from_env(str(explicit_path))
    assert cfg.runner_label == "from_explicit_path"


def test_config_from_env_bad_file_raises_loudly(tmp_path, monkeypatch):
    path = _write(tmp_path, "not_a_real_field = 1\n")
    _base_env(monkeypatch)
    with pytest.raises(ConfigFileError):
        cli._config_from_env(str(path))


def test_config_from_env_personas_table_fills_personas_field(tmp_path, monkeypatch):
    path = _write(tmp_path, """
        [personas]
        skills_root = "/srv/skills"
    """)
    _base_env(monkeypatch)
    cfg = cli._config_from_env(str(path))
    assert isinstance(cfg.personas, PersonaResolverConfig)
    assert cfg.personas.skills_root == "/srv/skills"
