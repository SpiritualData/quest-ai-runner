"""What one chat turn (and one chat START) costs, pinned so the costs stay gone.

Each case here was measured on a real corpus before it was fixed: turn-start context assembly and
guidance selection routinely blew their 15s / 5s budgets, not because the budgets were small but
because every turn and every session start did work it had already done:

* topic discovery re-ran on ~20k files at every chat start (files it had sampled past never
  counted as seen);
* every refresh rewrote every card imported from a nested store (3,230 cards, 11,847 files);
* the per-turn freshness check read every pinned file in full and spawned ``git`` for each;
* the conversation scan walked every node_modules/venv in the tree (37s);
* selection-only LLM calls paid for hidden model reasoning (~760 thought tokens each).

Fully offline.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

from quest_ai_runner.adapters.file_context_store import FileContextStore


def _provider(topics: List[Dict[str, Any]]) -> MagicMock:
    p = MagicMock()
    p.list_models.return_value = []
    p.answer.return_value = json.dumps(topics)
    return p


def _bump(path: Path, text: str) -> None:
    """Rewrite ``path`` and move its mtime forward, so the change is visible to a stat check."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (repo / "pkg" / "utils.py").write_text("def slug(s):\n    return s\n", encoding="utf-8")
    (repo / "pkg" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    return repo


TOPIC = [{"id": "models", "name": "Models", "keywords": ["models", "user"],
          "summary": "The User model.", "files": ["pkg/models.py"]}]


def test_refresh_does_not_rediscover_files_a_pass_already_saw(tmp_path):
    repo = _repo(tmp_path)
    store = FileContextStore(str(tmp_path / "cards"), repo_root=str(repo), auto_bootstrap=False)
    provider = _provider(TOPIC)
    assert store.bootstrap(root=str(repo), provider=provider) == 1
    calls_after_bootstrap = provider.answer.call_count
    assert calls_after_bootstrap > 0

    # utils.py / extra.py were shown to discovery but pinned by no card. Unchanged, they must not
    # cost another LLM call on the next refresh.
    store.refresh_stale(root=str(repo), provider=provider)
    assert provider.answer.call_count == calls_after_bootstrap

    # A file that CHANGES is new content again and does get analysed.
    _bump(repo / "pkg" / "extra.py", "VALUE = 2\n")
    store.refresh_stale(root=str(repo), provider=provider)
    assert provider.answer.call_count > calls_after_bootstrap


def test_unchanged_imported_cards_are_not_rewritten_on_refresh(tmp_path):
    child = tmp_path / "product"
    (child / "pkg").mkdir(parents=True)
    model_file = child / "pkg" / "models.py"
    model_file.write_text("class User:\n    pass\n", encoding="utf-8")
    FileContextStore(str(child / ".quest-context"), repo_root=str(child),
                     auto_bootstrap=False).bootstrap(root=str(child), provider=_provider(TOPIC))

    parent = FileContextStore(str(tmp_path / "parent_cards"), repo_root=str(tmp_path),
                              auto_bootstrap=False)
    assert parent.bootstrap(root=str(tmp_path)) == 1
    assert parent.refresh_stale(root=str(tmp_path)) == 0, "an unchanged import was rewritten"

    _bump(model_file, "class User:\n    name = ''\n")
    assert parent.refresh_stale(root=str(tmp_path)) == 1, "a changed import was not refreshed"


def test_turn_freshness_check_spawns_no_git_and_still_sees_changes(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    store = FileContextStore(str(tmp_path / "cards"), repo_root=str(repo), auto_bootstrap=False,
                             confidence_threshold=0.0)
    store.bootstrap(root=str(repo), provider=_provider(TOPIC))

    import quest_ai_runner.adapters.file_context_store as fcs

    def no_subprocess(*a, **k):
        raise AssertionError("assemble() spawned a subprocess for a freshness check")

    monkeypatch.setattr(fcs.subprocess, "run", no_subprocess)
    first = store.assemble("user models", meta={})
    assert "models" in first.card_ids
    assert first.stale == []

    _bump(repo / "pkg" / "models.py", "class User:\n    email = ''\n")
    again = store.assemble("user models", meta={})
    assert "pkg/models.py" in again.stale


def test_current_sha_reads_nothing_when_mtime_matches(tmp_path):
    repo = _repo(tmp_path)
    store = FileContextStore(str(tmp_path / "cards"), repo_root=str(repo), auto_bootstrap=False)
    f = repo / "pkg" / "models.py"
    entry = {"path": "pkg/models.py", "sha256": "recorded", "mtime": f.stat().st_mtime}
    assert store._current_sha("pkg/models.py", entry) == "recorded"
    _bump(f, "changed\n")
    assert store._current_sha("pkg/models.py", entry) not in ("", "recorded")


def test_conversation_scan_skips_library_dirs_but_finds_real_ones(tmp_path):
    from quest_ai_runner.adapters.conversation_format import _find_conversation_dirs

    (tmp_path / "app" / ".claude").mkdir(parents=True)
    (tmp_path / "notes" / "conversations").mkdir(parents=True)
    (tmp_path / ".tool" / "conversations").mkdir(parents=True)
    (tmp_path / "web" / "node_modules" / "lib" / ".claude").mkdir(parents=True)
    (tmp_path / "venv" / "lib" / "site-packages" / "sdk" / "conversations").mkdir(parents=True)

    found = {p.relative_to(tmp_path).as_posix() for p in _find_conversation_dirs(tmp_path)}
    assert found == {"app/.claude", "notes/conversations", ".tool/conversations"}


def test_reasoning_hint_reaches_only_providers_that_take_it():
    from quest_ai_runner.core.adapters import answer_with_reasoning

    class Old:
        def answer(self, messages, *, model, system=None):
            return "old"

    class New:
        seen = None

        def answer(self, messages, *, model, system=None, reasoning=None):
            New.seen = reasoning
            return "new"

    assert answer_with_reasoning(Old(), [], model="m", reasoning="minimal") == "old"
    assert answer_with_reasoning(New(), [], model="m", reasoning="minimal") == "new"
    assert New.seen == "minimal"


def test_gemini_minimal_reasoning_sets_thinking_and_falls_back():
    from quest_ai_runner.adapters.gemini_provider import GeminiProvider

    client = MagicMock()
    GeminiProvider._generate(client, "gemini-x", "prompt", {}, "minimal")
    cfg = client.models.generate_content.call_args.kwargs["config"]
    assert cfg["thinking_config"] == {"thinking_level": "minimal"}

    client = MagicMock()
    client.models.generate_content.side_effect = [ValueError("thinking_level not supported"),
                                                  "ok"]
    assert GeminiProvider._generate(client, "gemini-x", "prompt", {}, "minimal") == "ok"
    assert client.models.generate_content.call_args.kwargs["config"] is None

    client = MagicMock()
    GeminiProvider._generate(client, "gemini-x", "prompt", {}, None)
    assert client.models.generate_content.call_args.kwargs["config"] is None


def test_startup_refresh_is_skipped_when_the_store_was_refreshed_recently(tmp_path, monkeypatch):
    from quest_ai_runner import config as cfgmod
    from quest_ai_runner.adapters.file_context_store import (
        _TFDFIDF_VERSION, _write_bootstrap_meta,
    )

    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "some-card.json").write_text('{"id": "some-card"}', encoding="utf-8")
    _write_bootstrap_meta(str(cards), 1, feature_versions={"tfdfidf": _TFDFIDF_VERSION})
    monkeypatch.setenv("QAR_REFRESH_MIN_INTERVAL_SECONDS", "1800")

    def run():
        keyword = MagicMock()
        keyword.refresh_stale.return_value = 0
        cfgmod._bootstrap_if_needed(keyword, root=str(tmp_path), cards_dir=str(cards))
        for t in list(cfgmod.threading.enumerate()):
            if t.name == "qar-refresh":
                t.join(timeout=5)
        return keyword.refresh_stale.call_count

    assert run() == 1, "no stamp yet: the startup refresh must run"
    assert (cards / "index-state" / "last_refresh").exists()
    assert run() == 0, "refreshed moments ago: a second start must not walk the corpus again"

    monkeypatch.setenv("QAR_REFRESH_MIN_INTERVAL_SECONDS", "0")
    assert run() == 1, "an interval of 0 refreshes on every start"


def test_one_card_write_reparses_and_reweights_only_that_card(tmp_path, monkeypatch):
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository

    repo = FilesystemCardRepository(str(tmp_path))
    for i in range(3):
        repo.write(f"c{i}", {"id": f"c{i}", "name": f"Card {i}", "keywords": [f"k{i}"]})
    first = repo.load_all()
    repo.write("c1", {"id": "c1", "name": "Card one, edited", "keywords": ["k1", "edited"]})
    second = repo.load_all()
    assert second["c0"] is first["c0"] and second["c2"] is first["c2"]
    assert second["c1"] is not first["c1"] and second["c1"]["name"] == "Card one, edited"

    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    store._repo = repo
    calls = []
    real = FileContextStore._card_term_weights
    monkeypatch.setattr(FileContextStore, "_card_term_weights",
                        lambda self, card: calls.append(card["id"]) or real(self, card))
    store._scoring_index(repo.load_all())
    assert sorted(calls) == ["c0", "c1", "c2"]
    calls.clear()
    repo.write("c2", {"id": "c2", "name": "Card two, edited", "keywords": ["k2"]})
    store._scoring_index(repo.load_all())
    assert calls == ["c2"]


def test_repository_revision_sees_a_write_without_statting_every_card(tmp_path):
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository

    repo = FilesystemCardRepository(str(tmp_path))
    repo.write("a", {"id": "a"})
    before = repo.revision()
    assert repo.revision() == before
    time.sleep(0.01)
    repo.write("b", {"id": "b"})
    assert repo.revision() != before


def test_concurrent_first_loads_parse_the_store_once(tmp_path):
    import threading
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository

    repo = FilesystemCardRepository(str(tmp_path))
    for i in range(5):
        repo.write(f"c{i}", {"id": f"c{i}", "name": f"Card {i}"})
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    loads = []
    real_load = store._repo.load_all

    def slow_load():
        loads.append(1)
        time.sleep(0.2)
        return real_load()

    store._repo.load_all = slow_load
    threads = [threading.Thread(target=store._load_all) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1, "two threads each parsed the whole store"


def test_prewarm_builds_the_cache_the_first_turn_uses(tmp_path, monkeypatch):
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository

    repo = FilesystemCardRepository(str(tmp_path))
    repo.write("c0", {"id": "c0", "name": "Grants due soon", "keywords": ["grants", "deadline"]})
    store = FileContextStore(str(tmp_path), auto_bootstrap=False)
    store.prewarm()
    calls = []
    monkeypatch.setattr(FileContextStore, "_card_term_weights",
                        lambda self, card: calls.append(card["id"]) or {})
    store._scoring_index(store._load_all())
    assert calls == [], "the first turn recomputed weights prewarm already built"
