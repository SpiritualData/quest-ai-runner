"""The context index at startup: when to bootstrap, the background index thread, embedders and the Qdrant card repository.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from pathlib import Path

from .adapters.file_context_store import (
    _BOOTSTRAP_META_FILE,
    _BOOTSTRAP_VERSION,
    _TFDFIDF_VERSION,
    _read_bootstrap_meta,
)
from .config_support import _log


def _cards_exist(cards_dir: str) -> bool:
    """True if the cards directory already holds at least one card file.

    Excludes the ``bootstrap_meta.json`` sidecar (algorithm metadata, not a card).
    """
    try:
        p = Path(cards_dir)
        return p.is_dir() and any(
            e.suffix == ".json" and not e.name.startswith(".")
            and e.name != _BOOTSTRAP_META_FILE
            for e in p.iterdir()
        )
    except OSError:
        return False


# --- Background context-index threads: OWNED, not fire-and-forget ---------------------------------
# ``_bootstrap_if_needed`` starts the index build/refresh on a daemon thread so chat is usable
# immediately. Daemon alone is not ownership: the thread can (and did) outlive whatever started it,
# still walking a corpus and shelling out ``git hash-object`` per file long after nobody will read
# the result. Every thread started here is registered with the store it is indexing, so
# ``shutdown_background_index()`` can close the store (stopping it at its next checkpoint) and join
# the thread. Entries are dropped once the thread is finished, so this never grows without bound.
_INDEX_THREADS: "List[Tuple[threading.Thread, Any]]" = []


_INDEX_THREADS_LOCK = threading.Lock()


def _register_index_thread(thread: threading.Thread, store: Any) -> None:
    """Record a background index thread and the store it is writing, and forget finished ones."""
    with _INDEX_THREADS_LOCK:
        _INDEX_THREADS[:] = [(t, s) for (t, s) in _INDEX_THREADS if t.is_alive()]
        _INDEX_THREADS.append((thread, store))


def shutdown_background_index(timeout: float = 10.0) -> None:
    """Stop every background context-index thread this process started, and wait for them to exit.

    Calls ``close()`` on each store being indexed (bootstrap/refresh then stop at their next
    checkpoint and spawn no further ``git`` subprocess) and joins each thread, up to ``timeout``
    seconds in total. Cards already written are kept; indexing is incremental and resumes on the
    next start.

    Call this whenever the owner of an orchestrator goes away: a consumer rebuilding its wiring, a
    long-lived service shutting a tenant down, a CLI about to exit, a test finishing. Without it, an
    index pass belongs to nobody and its stray subprocesses land in whatever the process does next.
    Idempotent and never raises.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    with _INDEX_THREADS_LOCK:
        pending = list(_INDEX_THREADS)
        _INDEX_THREADS.clear()
    for _, store in pending:
        try:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 — shutdown is best-effort, never raises
            _log.debug("context index: closing store during shutdown failed", exc_info=True)
    for thread, _ in pending:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining = 0.1  # always give a closed thread a moment to notice and unwind
            thread.join(timeout=remaining)
            if thread.is_alive():
                _log.warning("context index: background thread %s did not stop within the timeout",
                             thread.name)
        except Exception:  # noqa: BLE001
            _log.debug("context index: joining background thread failed", exc_info=True)


def _refresh_min_interval_seconds() -> float:
    """``QAR_REFRESH_MIN_INTERVAL_SECONDS`` (default 1800): the shortest gap between two startup
    index refreshes of one store, across every process sharing it. 0 refreshes on every start."""
    try:
        return max(0.0, float(os.getenv("QAR_REFRESH_MIN_INTERVAL_SECONDS", "1800")))
    except ValueError:
        return 1800.0


def _bootstrap_if_needed(
    keyword, *, root: str, cards_dir: str, provider=None, model: Optional[str] = None,
    notify: Optional[Callable[[str], None]] = None, card_repository=None,
) -> None:
    """Bootstrap or refresh the keyword store at startup. Always runs in the background.

    * **Cards exist**: launch a background thread calling ``refresh_stale()`` — re-indexes
      only changed/new files and skips everything unchanged.

    * **No cards** (first run): bootstrap the full tree in a background thread. The LLM
      identifies semantic topic cards; the user can chat immediately against whatever cards
      exist so far (even none). The vector arm seeds lazily on first assemble() call.

    ``notify`` (optional): a callable that receives a user-visible status string. Callers
    that have a console (e.g. the interactive CLI) pass ``console.dim`` so bootstrap events
    appear as system messages; background-only callers leave it ``None`` and events are
    logged instead.

    ``card_repository`` with a ``health()`` (the Quest backend): the cards live THERE, so the local
    ``cards_dir`` says nothing about whether this corpus is indexed. Such a lane never runs a full
    first-time bootstrap (which would re-card and re-embed every file on the server just because the
    local folder is empty); it always takes the refresh path, which cards only files the backend's
    cards do not already cover or that changed. While the backend is unreachable it indexes nothing.
    """

    def _tell(msg: str) -> None:
        """Emit msg to the notify callback when set, else to the log."""
        if notify is not None:
            notify(msg)
        else:
            _log.info(msg)

    repository_backed = card_repository is not None and callable(getattr(card_repository, "health", None))
    if repository_backed and card_repository.health().get("degraded"):
        _log.error("context index: the card backend is unreachable (%s); not indexing this corpus "
                   "until it is back, so nothing is re-carded locally or pushed twice",
                   card_repository.health().get("last_error"))
        return

    if repository_backed or _cards_exist(cards_dir):
        # Check the global version and per-feature versions independently.
        # Global version bump → full LLM re-index for uncovered/stale files.
        # Per-feature version behind → cheap targeted migration (no LLM) until all cards updated.
        # Each is detected and handled separately so an unrelated feature never forces extra work.
        meta = _read_bootstrap_meta(cards_dir)
        stored_version = meta.get("version", 0)
        stored_features = meta.get("feature_versions", {})

        # A repository-backed lane's version markers live with the cards on the backend, not in a
        # local meta file it may never have had, so it never forces a full re-index from here.
        needs_full = not repository_backed and stored_version < _BOOTSTRAP_VERSION
        needs_tfdfidf = not repository_backed and stored_features.get("tfdfidf", 0) < _TFDFIDF_VERSION

        if needs_full:
            _tell(
                f"Context index: algorithm updated (v{stored_version} -> v{_BOOTSTRAP_VERSION}),"
                " re-indexing in background. Chat is ready now."
            )
        elif needs_tfdfidf:
            _tell(
                "Context index: computing tfdfidf signatures in background. Chat is ready now."
            )

        if needs_full or needs_tfdfidf:
            # Fall through to the background bootstrap path below. The bootstrap detects which
            # cards need tfdfidf migration and processes only those; per-entry "tfdfidf_v" is the
            # checkpoint so an interrupted run resumes from where it left off next startup.
            pass
        else:
            # Everything up to date: just refresh cards whose source files changed -- unless any
            # process sharing this store already did so recently. A refresh walks the whole
            # corpus several times over (nested-repo detection, folder review, the file walk,
            # nested-store discovery): ~40s of in-process CPU on a large corpus, competing with
            # the first turns of every chat session for the GIL. A session restarted minutes
            # later gains nothing from it: each turn's own freshness check already flags a card
            # whose files changed since it was written.
            stamp = Path(cards_dir) / "index-state" / "last_refresh"
            min_interval = _refresh_min_interval_seconds()
            try:
                age = time.time() - stamp.stat().st_mtime
            except OSError:
                age = None
            if age is not None and 0 <= age < min_interval:
                _log.debug("context index: refreshed %.0fs ago (< %.0fs); skipping startup refresh",
                           age, min_interval)
                return
            _log.debug("context index: scanning %s for changes (background)", root)

            def _bg_refresh() -> None:
                # ONE refresh per folder at a time, across every process sharing this store (both
                # SD lanes and the terminal start together on the daily restart). The stamp above
                # is only written when a refresh FINISHES, so without this lock two processes both
                # saw "stale", both found the same new files, and each asked the model for cards
                # for them: duplicate backfill and duplicate cards. A process that loses the race
                # skips: the winner's result lands in the shared card files it reads anyway.
                lock_fd = None
                try:
                    Path(cards_dir).mkdir(parents=True, exist_ok=True)
                    lock_fd = open(os.path.join(cards_dir, ".bootstrap.lock"), "w")  # noqa: WPS515
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        _log.debug("context index: another process is refreshing %s; skipping",
                                   cards_dir)
                        return
                    # Re-check under the lock: a process that held it a moment ago may have just
                    # finished and stamped, in which case there is nothing left to do.
                    try:
                        recent = 0 <= time.time() - stamp.stat().st_mtime < min_interval
                    except OSError:
                        recent = False
                    if recent:
                        return
                    n = keyword.refresh_stale(root=root, provider=provider, model=model)
                    try:
                        stamp.parent.mkdir(parents=True, exist_ok=True)
                        stamp.touch()
                    except OSError:
                        pass
                    if n > 0:
                        _log.info("context index: refreshed %d card(s)", n)
                        _tell(f"Context index updated: {n} card(s) refreshed.")
                    else:
                        _log.debug("context index: all cards up to date")
                except Exception:  # noqa: BLE001
                    _log.debug("context index: refresh failed", exc_info=True)
                finally:
                    if lock_fd is not None:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                            lock_fd.close()
                        except OSError:
                            pass

            _refresh_thread = threading.Thread(target=_bg_refresh, daemon=True, name="qar-refresh")
            _register_index_thread(_refresh_thread, keyword)
            _refresh_thread.start()
            return

    else:
        # First run — no cards yet.
        _tell(
            "Context index: building for the first time in background."
            " Chat is ready now; context improves as indexing completes."
        )

    # A lock file prevents duplicate bootstraps when multiple sessions start simultaneously.
    lock_path = os.path.join(cards_dir, ".bootstrap.lock")

    def _bg() -> None:
        try:
            Path(cards_dir).mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "w")  # noqa: WPS515 — intentional file-descriptor lifetime
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _tell("Context index: another session is already building the index, skipping.")
                lock_fd.close()
                return
            try:
                n = keyword.bootstrap(root=root, provider=provider, model=model)
                _log.info("context index: ready — %d cards written to %s", n, cards_dir)
                if n > 0:
                    calls = getattr(provider, 'call_count', 0)
                    msg = f"Context index ready: {n} card(s) indexed"
                    if calls > 0:
                        msg += f" ({calls} LLM calls)"
                    msg += "."
                    _tell(msg)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:  # noqa: BLE001
            _log.debug("context index: bootstrap failed", exc_info=True)

    _bootstrap_thread = threading.Thread(target=_bg, daemon=True, name="qar-bootstrap")
    _register_index_thread(_bootstrap_thread, keyword)
    _bootstrap_thread.start()


# --- Env vars the CARD-PERSISTENCE backend reads (documented here so config is discoverable) -------
# QAR_CARDS_BACKEND      — "file" (DEFAULT) or "qdrant". "file" keeps today's per-card-JSON behavior
#                          byte-for-byte. "qdrant" persists cards as points in ONE Qdrant collection
#                          via the generic ``QdrantCardRepository`` (no cards_dir), with a query-only
#                          ``QdrantCardVectorStore`` as the vector arm (each card embedded once).
# QAR_CARDS_COLLECTION   — (qdrant) the Qdrant collection cards live in (default "quest_ai_cards").
# QAR_QDRANT_URL         — (qdrant) the Qdrant server url; falls back to QAR_VECTOR_QDRANT_URL. When
#                          unset, an EMBEDDED local Qdrant under ``<cards_dir>/cards-qdrant`` is used.
# QAR_QDRANT_API_KEY     — (qdrant) the Qdrant api key; falls back to QAR_VECTOR_QDRANT_API_KEY.
# QAR_EMBEDDER_BACKEND   — (qdrant) "voyage" | "openai" | (unset/fastembed); selects the card embedder
#                          (same selector the auto vector store uses). Voyage uses asymmetric
#                          document/query embedders matching a backend's production setup.
# VOYAGE_EMBEDDING_SIZE  — (qdrant, voyage) the embedding dimension (default 1024).


def _qar_embedders(backend: str) -> tuple:
    """Resolve (doc_embedder, query_embedder, vector_size) for ``QAR_EMBEDDER_BACKEND``.

    "voyage" → asymmetric Voyage document/query embedders (dim from VOYAGE_EMBEDDING_SIZE, default
    1024); "openai" → one symmetric OpenAI embedder for both roles (dim 1536); anything else (unset
    or "fastembed") → the local fastembed default (one symmetric callable, dim 384). Returns
    ``(None, None, 0)`` on any failure so the caller can degrade. Never raises.
    """
    try:
        if backend == "voyage":
            from .adapters.qdrant_vector_store import make_voyage_embedder
            try:
                vsize = int(os.getenv("VOYAGE_EMBEDDING_SIZE") or 1024)
            except (TypeError, ValueError):
                vsize = 1024
            return (
                make_voyage_embedder(input_type="document"),
                make_voyage_embedder(input_type="query"),
                vsize,
            )
        if backend == "openai":
            from .adapters.qdrant_vector_store import make_openai_embedder
            emb = make_openai_embedder()
            return (emb, emb, 1536)
        # Local fastembed default (symmetric).
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

        def _fastembed(texts):
            return [list(v) for v in _model.embed(texts)]

        return (_fastembed, _fastembed, 384)
    except Exception:  # noqa: BLE001
        return (None, None, 0)


def _build_qdrant_card_repo_from_env(cards_dir: str) -> tuple:
    """Build a ``(QdrantCardRepository, QdrantCardVectorStore)`` pair from env, or ``(None, None)``.

    Reads ``QAR_CARDS_COLLECTION`` / ``QAR_QDRANT_URL`` / ``QAR_QDRANT_API_KEY`` and the
    ``QAR_EMBEDDER_BACKEND`` embedder. When no url is set, an EMBEDDED local Qdrant under
    ``<cards_dir>/cards-qdrant`` is used. Returns ``(None, None)`` on any failure (caller degrades to
    the filesystem backend). Never raises. NOTE: this default wiring is UNSCOPED (single-tenant) —
    a multi-tenant consumer wires its own scoped ``QdrantCardRepository`` per tenant instead.
    """
    try:
        from .adapters import QdrantCardRepository, QdrantCardVectorStore
        if QdrantCardRepository is None:  # [qdrant] extra not installed
            return (None, None)
        collection = (os.getenv("QAR_CARDS_COLLECTION") or "").strip() or "quest_ai_cards"
        url = (os.getenv("QAR_QDRANT_URL") or os.getenv("QAR_VECTOR_QDRANT_URL") or "").strip() or None
        api_key = (
            os.getenv("QAR_QDRANT_API_KEY") or os.getenv("QAR_VECTOR_QDRANT_API_KEY") or ""
        ).strip() or None
        path = None if url else os.path.join(cards_dir, "cards-qdrant")
        backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
        doc_emb, qry_emb, vsize = _qar_embedders(backend)
        if doc_emb is None or qry_emb is None:
            _log.warning("context index: QAR_CARDS_BACKEND=qdrant but no embedder could be built; "
                         "falling back to the file backend")
            return (None, None)
        repo = QdrantCardRepository(
            collection=collection, embedder=doc_emb, vector_size=vsize,
            client=None, url=url, api_key=api_key, path=path,
        )
        vstore = QdrantCardVectorStore(
            collection=collection, query_embedder=qry_emb,
            client=None, url=url, api_key=api_key, path=path,
        )
        return (repo, vstore)
    except Exception:  # noqa: BLE001
        _log.warning("context index: QAR_CARDS_BACKEND=qdrant repo build failed; falling back to "
                     "the file backend", exc_info=True)
        return (None, None)
