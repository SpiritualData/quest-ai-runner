"""Declarative ``notion`` and ``google_chat`` blocks: built only when they can work, fail closed.

What this file pins down:

  * BUILT ONLY WITH CREDENTIALS. A Notion block with no token available (env var unset, file
    missing) or no databases wires nothing and logs why; a Google Chat block with no service-account
    file wires nothing.
  * GOOGLE CHAT FAILS CLOSED: a block with no ``space_names`` wires nothing and the log says why,
    because delegation can otherwise read every space the subject is in. Scopes default read-only.
  * NO SECRET IN THE FILE OR THE LOG: Notion takes the NAME of an environment variable or a file
    path; the token value never appears in a log line.
  * ONE OBJECT, TWO USES: the same adapter is folded into the retrieval stack and handed to the
    update engine, so the new ``notion_database`` / ``google_chat`` sources work from config alone.
  * The keys are TOML-expressible, the live adapter fields are not.

Offline.
"""
import logging

import pytest

from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter
from quest_ai_runner.adapters.google_chat_adapter import DEFAULT_CHAT_SCOPES, GoogleChatAdapter
from quest_ai_runner.adapters.notion_adapter import NotionAdapter
from quest_ai_runner.adapters.reference_resolver import collect_reference_resolvers
from quest_ai_runner.config import (
    ConfigFileError,
    RunnerConfig,
    fold_into_retrieval,
    resolve_config_objects,
)
from quest_ai_runner.runner.context_updates import build_update_engine

DB = "0123456789abcdef0123456789abcdef"
TOKEN_VALUE = "tok-value-must-never-be-logged"


def notion_block(**kw):
    block = {"token_env": "QAR_TEST_NOTION_TOKEN", "database_ids": {"tasks": DB}}
    block.update(kw)
    return block


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("QAR_TEST_NOTION_TOKEN", TOKEN_VALUE)


@pytest.fixture
def sa_file(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text("{}")
    return str(path)


# --- notion --------------------------------------------------------------------------------------

def test_notion_is_built_from_an_env_var_name_and_a_database_map(token_env):
    cfg = resolve_config_objects(RunnerConfig(notion=notion_block()))
    assert isinstance(cfg.notion_adapter, NotionAdapter)
    assert cfg.notion_adapter.databases == {"tasks": DB}
    assert cfg.notion_adapter.token_provider() == TOKEN_VALUE


def test_notion_is_built_from_a_token_file(tmp_path):
    token_file = tmp_path / "notion.token"
    token_file.write_text(TOKEN_VALUE + "\n")
    cfg = resolve_config_objects(RunnerConfig(
        notion={"token_file": str(token_file), "database_ids": {"tasks": DB}}))
    assert cfg.notion_adapter is not None and cfg.notion_adapter.token_provider() == TOKEN_VALUE


def test_notion_with_no_token_available_wires_nothing_and_says_why(monkeypatch, caplog):
    monkeypatch.delenv("QAR_TEST_NOTION_TOKEN", raising=False)
    with caplog.at_level(logging.INFO):
        cfg = resolve_config_objects(RunnerConfig(notion=notion_block()))
    assert cfg.notion_adapter is None
    assert "no token available" in caplog.text and "QAR_TEST_NOTION_TOKEN" in caplog.text


def test_notion_with_a_missing_token_file_wires_nothing(tmp_path):
    cfg = resolve_config_objects(RunnerConfig(
        notion={"token_file": str(tmp_path / "absent"), "database_ids": {"tasks": DB}}))
    assert cfg.notion_adapter is None


def test_notion_with_no_databases_or_no_token_source_wires_nothing(token_env, caplog):
    for block in ({"token_env": "QAR_TEST_NOTION_TOKEN"},
                  {"token_env": "QAR_TEST_NOTION_TOKEN", "database_ids": {}},
                  {"token_env": "QAR_TEST_NOTION_TOKEN", "database_ids": {"tasks": "not-an-id"}},
                  {"database_ids": {"tasks": DB}}):
        assert resolve_config_objects(RunnerConfig(notion=block)).notion_adapter is None


def test_the_notion_token_value_is_never_logged(token_env, caplog):
    with caplog.at_level(logging.DEBUG):
        resolve_config_objects(RunnerConfig(notion=notion_block()))
        resolve_config_objects(RunnerConfig(notion=notion_block(database_ids={})))
    assert TOKEN_VALUE not in caplog.text


# --- google chat ---------------------------------------------------------------------------------

def test_google_chat_is_built_read_only_with_its_allowlist(sa_file, monkeypatch):
    seen = {}

    def fake_provider(**kwargs):
        seen.update(kwargs)
        return lambda: "tok"

    monkeypatch.setattr("quest_ai_runner.adapters.google_chat_adapter.service_account_token_provider",
                        fake_provider)
    cfg = resolve_config_objects(RunnerConfig(google_chat={
        "service_account_file": sa_file, "subject": "someone@example.org",
        "space_names": ["spaces/AAAA1111"], "lookback_days": 7,
        "assistant_senders": ["users/assistant1"]}))
    adapter = cfg.google_chat_adapter
    assert isinstance(adapter, GoogleChatAdapter)
    assert adapter.allowed_spaces == ["spaces/AAAA1111"]
    assert adapter.space_allowed("spaces/AAAA1111") and not adapter.space_allowed("spaces/OTHER")
    assert seen["scopes"] == list(DEFAULT_CHAT_SCOPES)
    assert all(scope.endswith(".readonly") for scope in seen["scopes"])
    assert seen["subject"] == "someone@example.org"
    assert adapter.is_assistant_message({"sender": {"name": "users/assistant1"}})


def test_google_chat_without_space_names_fails_closed_and_says_why(sa_file, caplog):
    for block in ({"service_account_file": sa_file},
                  {"service_account_file": sa_file, "space_names": []},
                  {"service_account_file": sa_file, "space_names": ["", "  "]}):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            cfg = resolve_config_objects(RunnerConfig(google_chat=block))
        assert cfg.google_chat_adapter is None
        assert "space_names" in caplog.text and "every space" in caplog.text


def test_google_chat_needs_a_key_file_that_exists(tmp_path):
    for block in ({"space_names": ["spaces/A"]},
                  {"service_account_file": str(tmp_path / "absent.json"), "space_names": ["spaces/A"]}):
        assert resolve_config_objects(RunnerConfig(google_chat=block)).google_chat_adapter is None


def test_scopes_can_be_overridden_but_default_to_read_only(sa_file, monkeypatch):
    seen = {}
    monkeypatch.setattr("quest_ai_runner.adapters.google_chat_adapter.service_account_token_provider",
                        lambda **kw: seen.update(kw) or (lambda: "tok"))
    resolve_config_objects(RunnerConfig(google_chat={
        "service_account_file": sa_file, "space_names": ["spaces/A"], "scopes": ["custom-scope"]}))
    assert seen["scopes"] == ["custom-scope"]


def test_live_adapters_supplied_in_python_are_never_overwritten(token_env, sa_file):
    mine_notion, mine_chat = object(), object()
    cfg = resolve_config_objects(RunnerConfig(
        notion=notion_block(), notion_adapter=mine_notion,
        google_chat={"service_account_file": sa_file, "space_names": ["spaces/A"]},
        google_chat_adapter=mine_chat))
    assert cfg.notion_adapter is mine_notion and cfg.google_chat_adapter is mine_chat


# --- TOML ----------------------------------------------------------------------------------------

def test_the_blocks_are_expressible_in_a_toml_file_and_hold_no_secret(tmp_path):
    f = tmp_path / "qar.toml"
    f.write_text(
        '[notion]\ntoken_env = "NOTION_TOKEN"\n[notion.database_ids]\ntasks = "0123456789abcdef0123456789abcdef"\n'
        '[google_chat]\nservice_account_file = "/etc/chat-sa.json"\nsubject = "someone@example.org"\n'
        'space_names = ["spaces/AAAA1111"]\nlookback_days = 14\n')
    cfg = RunnerConfig.from_file(str(f))
    assert cfg.notion == {"token_env": "NOTION_TOKEN", "database_ids": {"tasks": DB}}
    assert cfg.google_chat["space_names"] == ["spaces/AAAA1111"] and cfg.google_chat["lookback_days"] == 14


@pytest.mark.parametrize("key", ["notion_adapter", "google_chat_adapter"])
def test_a_live_adapter_is_not_a_file_key(tmp_path, key):
    f = tmp_path / "qar.toml"
    f.write_text(f'{key} = "x"\n')
    with pytest.raises(ConfigFileError):
        RunnerConfig.from_file(str(f))


# --- one object, two uses ------------------------------------------------------------------------

def test_the_update_engine_is_handed_the_same_adapters_the_config_built(token_env, sa_file):
    cfg = resolve_config_objects(RunnerConfig(
        notion=notion_block(),
        google_chat={"service_account_file": sa_file, "space_names": ["spaces/AAAA1111"]}))
    engine = build_update_engine(cfg, None)
    assert engine._sources["notion_database"]._client is cfg.notion_adapter
    assert engine._sources["google_chat"]._client is cfg.google_chat_adapter


def test_an_engine_built_with_no_blocks_has_the_sources_unconfigured():
    engine = build_update_engine(RunnerConfig(), None)
    assert engine._sources["notion_database"]._client is None
    assert engine._sources["google_chat"]._client is None


def test_the_adapters_are_folded_into_retrieval_once_and_resolve_their_references(token_env, sa_file, tmp_path):
    cfg = resolve_config_objects(RunnerConfig(
        notion=notion_block(),
        google_chat={"service_account_file": sa_file, "space_names": ["spaces/AAAA1111"]}))
    files = FilesAdapter(str(tmp_path))
    cfg.retrieval = files
    fold_into_retrieval(cfg, [cfg.notion_adapter, cfg.google_chat_adapter])
    assert isinstance(cfg.retrieval, CompositeRetrievalAdapter)
    assert cfg.retrieval.adapters == [files, cfg.notion_adapter, cfg.google_chat_adapter]

    fold_into_retrieval(cfg, [cfg.notion_adapter, cfg.google_chat_adapter])      # idempotent
    assert len(cfg.retrieval.adapters) == 3
    assert set(collect_reference_resolvers(cfg.retrieval)) == {"notion_page", "chat_thread"}


def test_folding_into_an_empty_stack_and_a_single_adapter_stack(token_env):
    cfg = resolve_config_objects(RunnerConfig(notion=notion_block()))
    fold_into_retrieval(cfg, [cfg.notion_adapter])
    assert cfg.retrieval is cfg.notion_adapter
    fold_into_retrieval(cfg, [None])                                              # nothing to add
    assert cfg.retrieval is cfg.notion_adapter


def test_build_orchestrator_puts_the_configured_adapters_in_the_brains_retrieval(token_env, sa_file, monkeypatch):
    from tests.conftest import StubEscalation, StubProvider, StubRetrieval
    from quest_ai_runner.config import build_orchestrator

    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    cfg = resolve_config_objects(RunnerConfig(
        notion=notion_block(),
        google_chat={"service_account_file": sa_file, "space_names": ["spaces/AAAA1111"]},
        retrieval=StubRetrieval({"README.md": "hi"}),
        model_provider=StubProvider([]),
        model_fallback={"balanced": "gemini-3.5-flash"},
        escalation=StubEscalation(),
        deep_runner=None,
    ))
    orch = build_orchestrator(cfg)
    members = orch.retrieval.adapters
    assert cfg.notion_adapter in members and cfg.google_chat_adapter in members
    assert sum(isinstance(a, NotionAdapter) for a in members) == 1
