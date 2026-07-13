"""Background context-indexing is OWNED: no index thread outlives the owner that started it.

``config._bootstrap_if_needed`` builds/refreshes the context index on a daemon thread so chat is
usable immediately. Daemon is not ownership. The pass walks the corpus and runs ``git hash-object``
once per file, and nothing joined or cancelled it, so it kept walking (and kept shelling out to git)
long after whatever started it was gone. In a test suite that showed up as a stray
``git ... hash-object ...`` landing inside a LATER test that had patched ``subprocess`` or the
environment, failing a different test on each run. In a consumer it is the same defect wearing a
different hat: I/O and subprocesses burned for a store nobody will read again, and a rebuilt
orchestrator racing its own predecessor over the same cards directory.

The fix is ownership, not a test hack:

  * ``FileContextStore.close()`` stops that store's indexing at its next checkpoint and guarantees no
    further ``git`` subprocess is spawned;
  * ``config.shutdown_background_index()`` closes every store an index thread was started for and
    JOINS those threads, so after it returns the process has no index thread running.

(``tests/conftest.py`` calls the latter after every test, which is what makes the suite
deterministic. These tests hold the library behaviour that makes that call meaningful.)
"""
import subprocess
import threading
import time
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.config import _bootstrap_if_needed, shutdown_background_index


def _index_threads_alive():
    return [t for t in threading.enumerate()
            if t.name in ("qar-bootstrap", "qar-refresh") and t.is_alive()]


def _corpus(tmp_path: Path, n_files: int = 60) -> Path:
    """A small source tree for the indexer to walk."""
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(n_files):
        (root / f"mod_{i}.py").write_text(f"# module {i}\n\n\ndef f{i}():\n    return {i}\n")
    return root


def _store(tmp_path: Path, root: Path) -> FileContextStore:
    return FileContextStore(
        cards_dir=str(tmp_path / "cards"),
        repo_root=str(root),
        auto_bootstrap=False,   # this test drives bootstrap explicitly, never lazily
    )


# --------------------------------------------------------------------------- close()


def test_close_stops_a_bootstrap_and_spawns_no_further_git_subprocess(tmp_path, monkeypatch):
    """A closed store runs no ``git hash-object``: the stray-subprocess defect at its source."""
    root = _corpus(tmp_path)
    store = _store(tmp_path, root)
    store.close()

    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    # The whole indexing surface must be inert once closed, and must never raise.
    assert store.bootstrap(root=str(root)) == 0
    assert store.refresh_stale(root=str(root)) == 0
    assert store.is_closed() is True
    assert not any("git" in str(c) for c in calls), f"a closed store still shelled out: {calls}"


def test_close_is_idempotent_and_safe_from_any_thread(tmp_path):
    store = _store(tmp_path, _corpus(tmp_path, n_files=2))
    assert store.is_closed() is False
    store.close()
    store.close()
    threading.Thread(target=store.close).start()
    time.sleep(0.05)
    assert store.is_closed() is True


def test_an_open_store_still_fingerprints_normally(tmp_path):
    """Production behaviour is untouched: an OPEN store fingerprints (git blob sha included when the
    root is a repo, empty when it is not). Only ``close()`` suppresses it."""
    root = _corpus(tmp_path, n_files=1)
    store = _store(tmp_path, root)
    fp = store._fingerprint("mod_0.py")
    assert fp["sha256"], "an open store must still fingerprint its files"
    assert "git_sha" in fp        # present as a key; empty when the root is not a git repo


# --------------------------------------------------------------------------- shutdown_background_index


def test_shutdown_joins_the_background_index_thread(tmp_path):
    """After ``shutdown_background_index()`` returns, no index thread is left running: the exact
    guarantee the flaky suite lacked."""
    root = _corpus(tmp_path, n_files=200)
    store = _store(tmp_path, root)
    before = len(_index_threads_alive())

    # No provider: the pass still walks and diffs (the expensive part), it just writes no LLM cards.
    _bootstrap_if_needed(store, root=str(root), cards_dir=str(tmp_path / "cards"))
    assert len(_index_threads_alive()) >= before + 1, "the bootstrap thread never started"

    shutdown_background_index(timeout=10.0)

    assert store.is_closed() is True
    assert _index_threads_alive() == [], "an index thread outlived shutdown_background_index()"


def test_shutdown_is_idempotent_and_inert_with_nothing_running():
    # Safe to call when no index was ever started (a consumer that wires no context store).
    shutdown_background_index(timeout=1.0)
    shutdown_background_index(timeout=1.0)
    assert _index_threads_alive() == []


def test_no_index_thread_survives_this_test_file(tmp_path):
    """The conftest guard runs after every test; this asserts the invariant it enforces, in-band."""
    root = _corpus(tmp_path, n_files=20)
    store = _store(tmp_path, root)
    _bootstrap_if_needed(store, root=str(root), cards_dir=str(tmp_path / "cards"))
    shutdown_background_index(timeout=10.0)
    assert _index_threads_alive() == []
