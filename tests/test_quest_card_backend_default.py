"""The Quest backend is the DEFAULT card and vector backend when a lane has Quest credentials;
``QAR_CARDS_BACKEND=local`` is the opt-out that leaves every local card_store_* behaviour as it was.

Offline: a fake card API on localhost and a patched local vector store, so these prove which backend
``resolve_context_assembler`` wires and that nothing is embedded or indexed locally under Quest.
"""
import logging
from unittest.mock import MagicMock

import pytest

import quest_ai_runner.adapters as adapters_mod
from quest_ai_runner import config as config_mod
from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.quest_api_card_repository import (
    QuestApiCardRepository, QuestApiCardVectorStore, quest_card_settings_from_env)
from quest_ai_runner.config import RunnerConfig, resolve_context_assembler
from tests.quest_card_backend_fake import FakeCardBackend

ENV_NAMES = (
    "QAR_QUEST_API_URL", "QAR_QUEST_API_KEY", "QAR_USER_ID", "QAR_TEAM_ID",
    "QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID", "QAR_CARDS_BACKEND",
    "QAR_QDRANT_URL", "QAR_VECTOR_QDRANT_URL", "QDRANT_URL", "QAR_EMBEDDER_BACKEND",
    "QAR_VECTOR_BACKEND", "QAR_CARD_MOUNT", "QAR_RUNNER_ENV_ID", "QAR_ENV_ID",
)


@pytest.fixture
def backend():
    fake = FakeCardBackend()
    yield fake
    fake.close()


@pytest.fixture
def local_vector(monkeypatch):
    """Patch the local vector store class and embedder builders; tests assert on whether they ran."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    store_cls = MagicMock(name="QdrantVectorStore")
    monkeypatch.setattr(adapters_mod, "QdrantVectorStore", store_cls)
    monkeypatch.setattr("quest_ai_runner.config_assembler._bootstrap_if_needed", lambda *a, **k: None)
    embedder = MagicMock(name="make_voyage_embedder")
    import quest_ai_runner.adapters.qdrant_vector_store as qvs
    monkeypatch.setattr(qvs, "make_voyage_embedder", embedder)
    return store_cls, embedder


def resolve(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "main.py").write_text('"""Main."""\n', encoding="utf-8")
    provider = MagicMock()
    provider.list_models.return_value = []
    provider.answer.return_value = "[]"
    cfg = RunnerConfig(retrieval=None, model_provider=provider, corpus_root=str(root),
                       context_cards_dir=str(tmp_path / "cards"))
    return resolve_context_assembler(cfg)


def walk(assembler):
    """Every node of the assembler tree."""
    seen = [assembler]
    for child in getattr(assembler, "assemblers", None) or []:
        seen.extend(walk(child))
    for attr in ("_keyword", "keyword", "_vector", "vector"):
        child = getattr(assembler, attr, None)
        if child is not None and child is not assembler:
            seen.extend(walk(child))
    return seen


def card_store(assembler):
    return next(n for n in walk(assembler) if isinstance(n, FileContextStore))


def test_settings_prefer_the_qar_names_over_the_lane_names():
    env = {"QAR_QUEST_API_URL": "http://qar.test", "QUEST_BASE_URL": "http://lane.test",
           "QUEST_API_KEY": "lane_key", "QUEST_TEAM_ID": "team_lane", "QAR_TEAM_ID": "team_qar"}
    got = quest_card_settings_from_env(env)
    assert got["base_url"] == "http://qar.test" and got["api_key"] == "lane_key"
    assert got["team_id"] == "team_qar"


def test_lane_env_names_alone_select_the_quest_backend(tmp_path, monkeypatch, local_vector, backend):
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    store = card_store(resolve(tmp_path))
    repo = store._repo
    assert isinstance(repo, QuestApiCardRepository)
    assert repo.team_id == "team_a" and repo.user_id == "user_a"      # resolved from the key (whoami)
    assert any(r[1] == "/api/teams/whoami" for r in backend.requests)


def test_qar_names_win_when_both_are_set(tmp_path, monkeypatch, local_vector, backend):
    monkeypatch.setenv("QAR_QUEST_API_URL", backend.url)
    monkeypatch.setenv("QAR_QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_BASE_URL", "http://unused.test")
    monkeypatch.setenv("QUEST_API_KEY", "other")
    monkeypatch.setenv("QAR_TEAM_ID", "team_qar")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_lane")
    repo = card_store(resolve(tmp_path))._repo
    assert repo.base_url == backend.url and repo.team_id == "team_qar"


def test_no_credentials_means_local_cards_exactly_as_before(tmp_path, local_vector):
    store = card_store(resolve(tmp_path))
    assert isinstance(store._repo, FilesystemCardRepository)
    assert local_vector[0].called          # the local vector arm is still built


@pytest.mark.parametrize("value", ["local", "file", "LOCAL"])
def test_local_is_the_opt_out_even_with_credentials(tmp_path, monkeypatch, local_vector, backend, value):
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QAR_CARDS_BACKEND", value)
    store = card_store(resolve(tmp_path))
    assert isinstance(store._repo, FilesystemCardRepository)
    assert local_vector[0].called
    assert not backend.requests            # the backend was never contacted


def test_under_the_quest_backend_nothing_is_embedded_or_indexed_locally(tmp_path, monkeypatch, local_vector, backend):
    store_cls, embedder = local_vector
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QAR_EMBEDDER_BACKEND", "voyage")
    assembler = resolve(tmp_path)
    assert not store_cls.called and not embedder.called
    assert any(isinstance(n, QuestApiCardVectorStore) or
               isinstance(getattr(n, "_store", None), QuestApiCardVectorStore) for n in walk(assembler))


def test_explicit_quest_without_credentials_is_loud_and_falls_back_to_local(tmp_path, monkeypatch, local_vector, caplog):
    monkeypatch.setenv("QAR_CARDS_BACKEND", "quest")
    notes = []
    with caplog.at_level(logging.ERROR):
        root = tmp_path / "repo"
        root.mkdir()
        cfg = RunnerConfig(retrieval=None, model_provider=MagicMock(), corpus_root=str(root),
                           context_cards_dir=str(tmp_path / "cards"))
        assembler = resolve_context_assembler(cfg, notify=notes.append)
    assert isinstance(card_store(assembler)._repo, FilesystemCardRepository)
    assert "QAR_CARDS_BACKEND=quest" in caplog.text and notes


def test_a_failing_whoami_is_loud_but_does_not_silently_go_local(tmp_path, monkeypatch, local_vector, backend, caplog):
    backend.whoami_status = 401
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "bad")
    with caplog.at_level(logging.ERROR):
        store = card_store(resolve(tmp_path))
    assert isinstance(store._repo, QuestApiCardRepository)
    assert store._repo.error_count >= 1 and "whoami" in caplog.text


def test_cards_written_through_the_store_carry_the_lane_team(tmp_path, monkeypatch, local_vector, backend):
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    repo = card_store(resolve(tmp_path))._repo
    repo.write("note-1", {"name": "Note", "summary": "s"})
    assert ("team_a", "note-1") in backend.cards


# --- bootstrap decision for a Quest-backed lane ---------------------------------------------------

class RecordingKeyword:
    """Records which indexing pass the bootstrap decision chose."""

    def __init__(self):
        self.calls = []

    def bootstrap(self, **kw):
        self.calls.append(("bootstrap", kw.get("skip_unchanged", False)))
        return 0

    def refresh_stale(self, **kw):
        self.calls.append(("refresh", True))
        return 0


class FakeRepository:
    def __init__(self, degraded=False):
        self.degraded = degraded

    def health(self):
        return {"ok": not self.degraded, "degraded": self.degraded, "error_count": int(self.degraded),
                "last_error": "unreachable" if self.degraded else None}


def run_decision(tmp_path, monkeypatch, repository):
    import threading
    started = []
    real_thread = threading.Thread

    def run_inline(*a, target=None, **k):
        t = real_thread(*a, target=target, **k)
        started.append(t)
        return t

    monkeypatch.setattr(config_mod.threading, "Thread", run_inline)
    keyword = RecordingKeyword()
    empty_cards = tmp_path / "empty-cards"
    config_mod._bootstrap_if_needed(keyword, root=str(tmp_path), cards_dir=str(empty_cards),
                                    card_repository=repository)
    for t in started:
        t.join(timeout=10)
    return keyword.calls


def test_quest_backed_lane_with_empty_local_folder_refreshes_not_full_bootstrap(tmp_path, monkeypatch):
    calls = run_decision(tmp_path, monkeypatch, FakeRepository())
    assert calls == [("refresh", True)]


def test_quest_backed_lane_indexes_nothing_while_backend_unreachable(tmp_path, monkeypatch):
    assert run_decision(tmp_path, monkeypatch, FakeRepository(degraded=True)) == []


def test_local_lane_with_empty_folder_still_does_a_first_bootstrap(tmp_path, monkeypatch):
    calls = run_decision(tmp_path, monkeypatch, None)
    assert calls and calls[0][0] == "bootstrap"
