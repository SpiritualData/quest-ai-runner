"""Resolve the context assembler for a ``RunnerConfig``: which cards, vectors and turn store feed a turn.

Split out of ``config.py`` (re-exported there, so ``config.resolve_context_assembler`` keeps working).
The function reads the card backend, the vector backend and the bootstrap state from the environment
and the config, and returns the composite assembler, or None when none can be built. Which vector
store class fills the slot is chosen by ``core.resolve`` through ``cfg.selections``.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import TYPE_CHECKING, Callable, Optional

from typing import Any, List

from esoi import implementation
from hubofhubs.recall.card_repository import FilesystemCardRepository
from hubofhubs.recall.composite_assembler import CompositeContextAssembler
from hubofhubs.recall.reference_resolver import NoteResolver

from .config_context_index import (
    _bootstrap_if_needed,
    _build_qdrant_card_repo_from_env,
    _register_index_thread,
)
from .config_environment import resolve_context_assembler_stack
from .config_support import _AUTO_CONTEXT, _log
from .core.resolve import resolve

if TYPE_CHECKING:  # typing only: config imports this module, not the other way round
    from .config import RunnerConfig


@implementation("select-context", "composite")
def composite(assemblers: List[Any]) -> CompositeContextAssembler:
    """QAR's ``select-context`` arm that runs several assemblers in order as one: the hub's composite assembler."""
    return CompositeContextAssembler(assemblers)


@implementation("persist-cards", "filesystem")
def filesystem_card_repository(cards_dir: str) -> FilesystemCardRepository:
    """QAR's ``persist-cards`` arm that keeps one JSON file per card in a folder: the hub's filesystem card repository."""
    return FilesystemCardRepository(cards_dir)


@implementation("resolve-references", "note")
def note_resolver() -> NoteResolver:
    """QAR's ``resolve-references`` built-in for ``note`` items: the hub's note resolver (the registry itself is the hub's ``build_resolver_registry``)."""
    return NoteResolver()


def build_card_repository(cfg: "RunnerConfig", cards_dir: str, *,
                          notify: Optional[Callable[[str], None]] = None):
    """``(repository, query-only vector store, on the Quest backend)`` for a lane's cards, from its environment.

    ``(None, None, False)`` is the default local folder store. The runner's context assembler and the ``cards``
    commands both ask this, so a command run by hand reaches the same store the lane does. A bad card scope
    raises ``CardScopeError`` (a startup failure, never a quiet fallback); any other failure to reach the
    Quest backend logs an ERROR and falls back to the local store.
    """
    # CARD-PERSISTENCE backend. The QUEST BACKEND is the DEFAULT whenever Quest credentials are in
    # the environment (QAR_QUEST_API_URL|QUEST_BASE_URL + QAR_QUEST_API_KEY|QUEST_API_KEY): cards
    # and their vectors live there, are embedded once server-side and searched server-side, and a
    # lane embeds and indexes no card locally. The acting user is whatever the key authenticates
    # (whoami). Cards a lane writes carry its team (QAR_TEAM_ID|QUEST_TEAM_ID).
    #   QAR_CARDS_BACKEND=local (alias "file"): the OPT-OUT. Per-card JSON files under cards_dir,
    #       with every local card_store_* behaviour (mounts, merge-into, move-root, read-only
    #       remote vectors) and the local auto-built vector arm exactly as before.
    #   QAR_CARDS_BACKEND=qdrant: one Qdrant collection (no cards_dir dependency).
    #   QAR_CARDS_BACKEND=quest: require the Quest backend. Without credentials this logs an ERROR,
    #       notifies, and runs LOCAL; the ERROR is the flag that it is not on the backend.
    _cards_backend = (os.getenv("QAR_CARDS_BACKEND") or "").strip().lower()
    if _cards_backend == "file":
        _cards_backend = "local"
    _card_repository = None
    _cards_vector_store = None  # query-only arm over the cards (qdrant backend, or the Quest backend)
    _quest_cards_active = False

    from .adapters.quest_api_card_repository import quest_card_settings_from_env
    _qs = quest_card_settings_from_env()
    _has_quest_creds = bool(_qs["base_url"] and _qs["api_key"])
    if _cards_backend == "quest" and not _has_quest_creds:
        _msg = ("context index: QAR_CARDS_BACKEND=quest but no Quest credentials are set "
                "(QAR_QUEST_API_URL|QUEST_BASE_URL and QAR_QUEST_API_KEY|QUEST_API_KEY); "
                "falling back to LOCAL cards, which are NOT shared with the Quest backend")
        _log.error(_msg)
        if notify:
            notify(_msg)
    elif _cards_backend in ("", "auto", "quest") and _has_quest_creds:
        # The lane's card scope (QAR_CARDS_SCOPE). A bad or unresolvable setting is a startup
        # failure: it is raised here, outside the build guard below, never a quiet fallback.
        from .adapters.quest_api_card_repository import (
            QuestApiCardRepository as ScopeProbe, select_card_scope)
        card_scope = select_card_scope(_qs, org_of_team=lambda lane_team: ScopeProbe(
            base_url=_qs["base_url"], api_key=_qs["api_key"],
            user_id=_qs["user_hint"]).org_of_team(lane_team))
        try:
            from .adapters.quest_api_card_repository import (
                QuestApiCardRepository, QuestApiCardVectorStore)
            # The offline listing cache is the "cache-cards" selection (durable file cache by
            # default), kept apart per backend and scope so lanes never share a listing.
            lane_key = hashlib.sha256("|".join((
                _qs["base_url"], card_scope["team_id"], card_scope["org_id"],
                _qs["user_hint"])).encode()).hexdigest()[:16]
            _card_cache = resolve("cache-cards", overrides=cfg.selections)(
                os.path.join(cards_dir, ".api-card-cache", lane_key))
            _card_repository = QuestApiCardRepository(
                base_url=_qs["base_url"],
                api_key=_qs["api_key"],
                user_id=_qs["user_hint"],
                team_id=card_scope["team_id"],
                org_id=card_scope["org_id"],
                on_error=notify,
                cache=_card_cache,
            )
            _user = _card_repository.resolve_user_id()
            _quest_cards_active = True
            _cards_vector_store = QuestApiCardVectorStore(_card_repository)
            _log.info(
                "context index: using the Quest backend for cards and vectors (%s, user %s, %s)",
                _qs["base_url"], (_user[:4] + "..." if _user else "UNRESOLVED"),
                (f"org {card_scope['org_id']}" if card_scope["org_id"]
                 else (f"team {card_scope['team_id']}" if card_scope["team_id"] else "private cards")),
            )
        except Exception:  # noqa: BLE001
            _log.error(
                "context index: QuestApiCardRepository build failed; falling back to the LOCAL "
                "card backend (cards are NOT on the Quest backend)", exc_info=True,
            )
            _card_repository = None
            _cards_vector_store = None
            _quest_cards_active = False
    elif _cards_backend == "qdrant":
        _card_repository, _cards_vector_store = _build_qdrant_card_repo_from_env(cards_dir)

    return _card_repository, _cards_vector_store, _quest_cards_active


def resolve_context_assembler(
    cfg: RunnerConfig,
    *,
    notify: Optional[Callable[[str], None]] = None,
):
    """Resolve the context assembler from config, then fuse on the declarative LAYERS.

    Two steps, because the layers WRAP a base rather than replace it: build (or accept) the base
    assembler, then apply ``vector_store`` and ``rep_context_prefs`` if the lane declared them.
    Assembling those layers by hand was the last reason a lane needed a Python consumer for its
    context stack. Each layer is skipped silently when unconfigured or unavailable.
    """
    return resolve_context_assembler_stack(
        _resolve_context_assembler_base(cfg, notify=notify), cfg)


def _resolve_context_assembler_base(
    cfg: RunnerConfig,
    *,
    notify: Optional[Callable[[str], None]] = None,
):
    """The base assembler — ON BY DEFAULT.

    Tri-state on ``cfg.context_assembler``:
      * ``_AUTO_CONTEXT`` (the field default, i.e. the consumer left it unset) → build the default
        ``FileContextStore`` so context handling works out of the box. Cards live under
        ``cfg.context_cards_dir`` or ``<corpus_root or cwd>/.quest-context``; ``repo_root`` is the
        same root so staleness can also read git blob shas. Construction never raises (a bad path
        just yields a store whose best-effort reads/writes no-op), so the runner still starts.
      * an INSTANCE → use it as-is (a Quest-backed or composite assembler, etc.).
      * ``None`` → context handling is explicitly DISABLED.
    """
    chosen = cfg.context_assembler
    if chosen is not _AUTO_CONTEXT:
        return chosen  # an explicit instance, or None to disable
    # Default-on: build a FileContextStore (keyword/IDF). Local import avoids a cycle.
    from .adapters.quest_api_card_repository import CardScopeError
    try:
        from .adapters import FileContextStore
        from .adapters.card_store_settings import max_cards_from_env
        root = cfg.corpus_root or os.getcwd()
        cards_dir = cfg.context_cards_dir or os.path.join(root, ".quest-context")
        # auto_bootstrap=False: we manage the lifecycle ourselves via
        # _bootstrap_if_needed so lazy and explicit bootstrap don't race.
        # provider= enables LLM relevance filtering of IDF candidates so only
        # cards genuinely relevant to the user's task are injected.
        from .core.model_registry import ModelRegistry as _MR
        _registry = _MR(cfg.model_provider, fallback=cfg.model_fallback or None)

        _card_repository, _cards_vector_store, _quest_cards_active = build_card_repository(
            cfg, cards_dir, notify=notify)
        if _card_repository is not None:   # the card-store seat, obtained through the hub assembly (the same repository object)
            from .hub import card_repository_of
            _card_repository = card_repository_of(_card_repository)

        # Auto-discover reference resolvers from any RESOLVABLE retrieval adapter the consumer wired
        # (structural: the adapter advertises a non-None ``reference_type`` + ``resolve_reference`` on
        # the RetrievalAdapter interface). Its OWN ``resolve_reference`` IS the resolver for its type,
        # so e.g. a wired ``GoogleChatAdapter`` makes its learned ``chat_thread`` references resolve
        # FRESH with no extra config. Consumer-supplied ``cfg.reference_resolvers`` win a collision.
        from hubofhubs.recall.reference_resolver import collect_reference_resolvers
        _store_resolvers = {
            **collect_reference_resolvers(cfg.retrieval),
            **(cfg.reference_resolvers or {}),
        }
        keyword = FileContextStore(
            cards_dir, repo_root=root, auto_bootstrap=False,
            provider=cfg.model_provider,
            model=_registry.resolve_tier("balanced"),
            # Source-agnostic card content: built-in file/note resolvers, plus the data-backed types
            # (collection/conversation/chat_thread/query) from resolvable retrieval adapters and any
            # consumer-injected ``cfg.reference_resolvers``.
            reference_resolvers=_store_resolvers,
            # qdrant backend: route ALL card persistence through the Qdrant repo (no cards_dir). None
            # (file backend) keeps the default FilesystemCardRepository(cards_dir).
            card_repository=_card_repository,
            # Same map the poller consults for quest<->folder QUEST_SYNC.md sync (see
            # RunnerConfig.quest_folder_map): boosts automated context selection toward a quest's
            # linked folder whenever a run's quest_id matches.
            quest_folder_map=cfg.quest_folder_map,
            # The runner's own store tidies provable copies at open and will not embed an inflated
            # store (adapters/context_store_heal); a library caller or a test builds without this.
            # It was off for a day (commit 6980d7c) while an independent review's findings were fixed: it
            # now moves only a card an index pass wrote and nobody touched, and joins what a copy holds onto
            # the card it copies first (card_origin.protection, card_store_hygiene.fold_card).
            self_heal=True,
            # What the automatic tidy through the repository port does (a store over its card limit, or holding
            # copies its ids prove): RunnerConfig.card_tidy, unset = apply on a local folder, plan elsewhere.
            tidy_mode=cfg.card_tidy,
            # The card limit is the runner's policy (QAR_MAX_CARDS, 1,000 when unset, 0 for none); the
            # library itself applies no limit unless its caller passes one.
            max_cards=max_cards_from_env(),
        )
        # If a vector store is configured, the default becomes a HYBRID: keyword/IDF FUSED with
        # semantic vector search (the two are complementary). Otherwise keyword-only.
        # Turn-history cards live alongside file cards: same root, subdir "turns".
        # This ensures turns are always at <corpus_root>/.quest-context/turns/ —
        # consistent regardless of cwd, and shared across CLI, chat, and the executor lane.
        from .core.turn_context_store import TurnContextStore
        turns_dir = os.path.join(cards_dir, "turns")
        turn_store = TurnContextStore(
            turns_dir=turns_dir,
            provider=cfg.model_provider,
            model=_registry.resolve_tier("balanced"),
            origin=keyword.origin_id(),
        )

        # Resolve the vector store: explicit instance > qdrant-cards query-only arm > auto-build
        # local Qdrant > None. For the qdrant CARD backend, the repo already embedded each card on
        # write, so its vector arm is QUERY-ONLY (the QdrantCardVectorStore over the SAME collection)
        # and must NOT be replaced by an auto-built embedding store. When the [qdrant] extra is
        # installed and no explicit store is configured (file backend), we auto-build an embedded
        # QdrantVectorStore (local filesystem, no server) so hybrid search is ON by default without
        # any consumer config. Keyword-only is the fallback when the extra is missing.
        #
        # QAR_VECTOR_BACKEND gates the AUTO-BUILT arm (it never overrides an explicit
        # cfg.vector_store or the qdrant card backend's query-only arm):
        #   "auto" (default/unset) — attempt Qdrant, fall back to keyword-only with a warning
        #                            when it cannot be opened (today's behavior).
        #   "none" / "off"        — skip the Qdrant attempt entirely: keyword-only, silent
        #                            (no construction attempt, no warning log).
        #   "qdrant"              — require Qdrant: log an ERROR when it cannot be opened
        #                            (still degrades to keyword-only so the runner starts).
        vector_backend = (os.getenv("QAR_VECTOR_BACKEND") or "").strip().lower() or "auto"
        if vector_backend not in ("auto", "none", "off", "qdrant"):
            _log.warning(
                "context index: unrecognized QAR_VECTOR_BACKEND=%r; treating as 'auto'",
                vector_backend,
            )
            vector_backend = "auto"
        vector_store = cfg.vector_store
        if vector_store is None and _cards_vector_store is not None:
            vector_store = _cards_vector_store
        if vector_store is None and vector_backend not in ("none", "off"):
            try:
                # Which store class: the declared implementations (core/resolve.py), by cfg.selections.
                _QdrantVS = resolve("index-by-meaning", overrides=cfg.selections)
                if _QdrantVS is not None:
                    if notify:
                        notify("Loading context index...")
                    # QAR_QDRANT_URL / QDRANT_URL: connect to a running Qdrant server.
                    # Preferred over embedded path — supports concurrent QAR instances.
                    # Falls back to an embedded local path when neither is set.
                    qdrant_url = (
                        os.getenv("QAR_QDRANT_URL")
                        or os.getenv("QAR_VECTOR_QDRANT_URL")
                        or os.getenv("QDRANT_URL")
                        or ""
                    ).strip() or None
                    qdrant_path = os.path.join(cards_dir, "qdrant")
                    # QAR_EMBEDDER_BACKEND selects the embedding backend for the auto-built
                    # vector store: "voyage" uses Voyage AI (asymmetric document/query embedders,
                    # matching the backend's production setup), anything else (unset or
                    # "fastembed") uses the bare store's default local fastembed embedder.
                    backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
                    _LOCAL_QDRANT_URL = "http://localhost:6333"

                    def _open_qdrant(**kw):
                        """Open QdrantVectorStore via URL (preferred) or embedded path.

                        URL mode (QAR_QDRANT_URL / QDRANT_URL set): connects to a running
                        Qdrant server — supports concurrent QAR instances with no locking.

                        Embedded mode (no URL): opens the local path. On lock contention
                        (another QAR instance holds it), falls back to localhost:6333 and,
                        if that is also unavailable, emits a visible warning and returns None.
                        """
                        if qdrant_url:
                            try:
                                return _QdrantVS(url=qdrant_url, **kw)
                            except Exception as e:  # noqa: BLE001
                                _log.warning("context index: Qdrant server at %s failed: %s",
                                             qdrant_url, e)
                                return None
                        # Embedded path.
                        try:
                            return _QdrantVS(path=qdrant_path, **kw)
                        except RuntimeError as e:
                            if "already accessed" not in str(e):
                                _log.warning("context index: Qdrant open failed: %s", e)
                                return None
                        except Exception as e:  # noqa: BLE001
                            _log.warning("context index: Qdrant open failed: %s", e)
                            return None
                        # Embedded path locked — try the local server as a fallback.
                        try:
                            store = _QdrantVS(url=_LOCAL_QDRANT_URL, **kw)
                            _log.info("context index: embedded Qdrant locked; connected to "
                                      "local server at %s", _LOCAL_QDRANT_URL)
                            return store
                        except Exception:  # noqa: BLE001
                            pass
                        msg = (
                            "Vector search unavailable: Qdrant path is locked by another QAR "
                            "instance and no local server responded at %s. "
                            "Set QDRANT_URL or QAR_QDRANT_URL to a running Qdrant server "
                            "(e.g. docker run -p 6333:6333 qdrant/qdrant)." % _LOCAL_QDRANT_URL
                        )
                        _log.warning("context index: %s", msg)
                        if notify:
                            notify(msg)
                        return None

                    if backend == "voyage":
                        try:
                            from .adapters.qdrant_vector_store import make_voyage_embedder, voyage_embedder_id
                            # Declare the REAL dimension up front. _open_qdrant used to omit
                            # vector_size here, so this always fell through to the constructor's
                            # 384 default and _adopt_embedding_dim logged a mismatch-and-adopt
                            # warning on every single process start once Voyage's actual 1024-dim
                            # output arrived -- correct behavior, but a warning that could never
                            # NOT fire is a config bug, not a real anomaly worth surfacing every
                            # run. Mirrors _qar_embedders' own VOYAGE_EMBEDDING_SIZE resolution.
                            try:
                                _voyage_vsize = int(os.getenv("VOYAGE_EMBEDDING_SIZE") or 1024)
                            except (TypeError, ValueError):
                                _voyage_vsize = 1024
                            vector_store = _open_qdrant(
                                vector_size=_voyage_vsize,
                                embedder=make_voyage_embedder(input_type="document"),
                                query_embedder=make_voyage_embedder(input_type="query"),
                                embedder_id=voyage_embedder_id(),
                            )
                        except Exception:  # noqa: BLE001 — voyageai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=voyage but the voyageai "
                                "embedder could not be built — falling back to fastembed",
                            )
                        if vector_store is None:
                            vector_store = _open_qdrant()
                    elif backend == "openai":
                        try:
                            from .adapters.qdrant_vector_store import make_openai_embedder, openai_embedder_id
                            embedder = make_openai_embedder()
                            # Same fix as the voyage branch above: declare the real (fixed) OpenAI
                            # embedding dimension up front instead of relying on the 384 default
                            # and a same-process "adopting 1536" warning every run.
                            vector_store = _open_qdrant(
                                vector_size=1536,
                                embedder=embedder,
                                query_embedder=embedder,
                                embedder_id=openai_embedder_id(),
                            )
                        except Exception:  # noqa: BLE001 — openai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=openai but the openai "
                                "embedder could not be built — falling back to fastembed",
                            )
                        if vector_store is None:
                            vector_store = _open_qdrant()
                    else:
                        vector_store = _open_qdrant()
            except (ImportError, Exception):  # noqa: BLE001
                # qdrant-client / fastembed not installed, or construction failed: keyword-only.
                vector_store = None
            if vector_store is None and vector_backend == "qdrant":
                _log.error(
                    "context index: QAR_VECTOR_BACKEND=qdrant but no Qdrant vector store "
                    "could be opened. Install the [qdrant] extra (qdrant-client + fastembed) "
                    "or point QAR_QDRANT_URL / QDRANT_URL at a running Qdrant server. "
                    "Falling back to keyword-only context search."
                )

        # Bootstrap (first run) or refresh stale cards (subsequent runs).
        # Always runs in the background — the vector arm seeds lazily on first
        # assemble() via seed_source, so blocking startup is never needed.
        # Resolve a balanced-tier model for the bootstrap LLM calls.
        from .core.model_registry import ModelRegistry
        registry = ModelRegistry(cfg.model_provider, fallback=cfg.model_fallback or None)
        bootstrap_model = registry.resolve_tier("balanced")
        _bootstrap_if_needed(keyword, root=root, cards_dir=cards_dir,
                             provider=cfg.model_provider, model=bootstrap_model, notify=notify,
                             card_repository=_card_repository)
        # Warm the card cache and scoring index while the session waits for its first message,
        # so the first turn's context budget is not spent parsing and weighting every card.
        if callable(getattr(keyword, "prewarm", None)):
            _prewarm_thread = threading.Thread(target=keyword.prewarm, kwargs={"notify": notify},
                                               daemon=True, name="qar-prewarm")
            _register_index_thread(_prewarm_thread, keyword)
            _prewarm_thread.start()

        if vector_store is not None:
            from .adapters import HybridContextAssembler, VectorContextAssembler
            # Wire seed_source so the vector arm is seeded from the keyword store's
            # docstring cards on the first assemble() call (cold-start fix). Because
            # sync() is fingerprint-based, subsequent calls only re-embed changed
            # cards (AUTO-UPDATE). Never raises: errors in seed_source() are swallowed
            # by VectorContextAssembler._maybe_seed().
            # Resolve the query model tier to a full model ID (not a bare tier name like "haiku")
            query_model = registry.resolve_tier("fast")
            # MOUNT MODE: the card arm may only see points of the stores this lane actually reads
            # (its own plus its readable mounts). The vector server answers anyone who can reach
            # it, so this payload filter is the fence; see adapters/card_store_fence.py.
            if getattr(keyword, "mounts_enabled", False):
                from .adapters.card_store_fence import StoreFencedVectorStore
                vector_store = StoreFencedVectorStore(
                    vector_store, keyword,
                    vector_space=(os.getenv("QAR_VECTOR_SPACE") or "").strip())
            vector = VectorContextAssembler(
                vector_store,
                provider=cfg.model_provider,
                query_model=query_model,
                # The hybrid below runs ONE holistic consolidating pass over the merged card set,
                # which judges these same vector hits as cards. Delegate the in-arm hit review to
                # it so hit relevance costs one model call per turn, not two (with no provider the
                # review was a no-op anyway, so this flag is safe unconditionally here).
                llm_review=False,
                seed_source=keyword.export_for_embedding,
                # The store ranks its cards and hands over only the best max_seed_items to embed.
                # Seeding used to run inside the first turn's vector arm and blow the assembly
                # budget; on its own thread it runs while the session waits for a first message.
                seed_in_background=True,
            )
            # Start it now so the store is warm while the session waits for its first message.
            vector.start_seed()
            # Wire the consolidating LLM pass: one holistic filter over the merged card set that
            # drops/reranks cards across arms and prunes their content items (content stays verbatim).
            # Uses the balanced tier (filtering/judgment work). Falls back to the mechanical merge
            # when no provider is wired or anything fails.
            consolidate_model = registry.resolve_tier("balanced")
            file_assembler = HybridContextAssembler(
                keyword=keyword,
                vector=vector,
                model_provider=cfg.model_provider,
                model=consolidate_model,
            )
        else:
            file_assembler = keyword

        # Claude session assembler: injects relevant Claude Code session digests
        # as pre-flight context alongside turn cards, using the same TF-DF-IDF
        # + recency selection as TurnContextStore.
        sessions_dir = os.getenv("QAR_CLAUDE_SESSIONS_DIR") or None
        try:
            from .adapters import ClaudeConversationsAdapter
            claude_assembler = ClaudeConversationsAdapter(
                corpus_root=root,
                sessions_dir=sessions_dir,
                # Wire the keyword card store so a cross-session recall hit becomes a LEARNED
                # ``conversation`` reference on the turn's active card (usage-recency tracked),
                # instead of being recomputed from the whole history every turn. Inert unless a
                # ``thread_card_id`` is in the assemble meta.
                card_store=keyword,
            )
            # The Claude adapter is itself resolvable (reference_type="conversation"); it is built
            # AFTER the store (it needs the store as its card_store), so it can't be discovered by
            # collect_reference_resolvers above. Register its resolver now so a LEARNED
            # ``conversation`` reference resolves FRESH by conv_id instead of dangling. Kept only if a
            # consumer/discovered ``conversation`` resolver isn't already wired (override=False).
            if getattr(claude_assembler, "reference_type", None):
                keyword.register_reference_resolver(
                    claude_assembler.reference_type,
                    claude_assembler.resolve_reference,
                )
        except Exception:  # noqa: BLE001
            claude_assembler = None

        assemblers = [file_assembler, turn_store]
        if claude_assembler is not None:
            assemblers.append(claude_assembler)
        return composite(assemblers)
    except CardScopeError:
        raise  # a bad QAR_CARDS_SCOPE must stop startup, not quietly run on another scope
    except Exception:  # noqa: BLE001 — never let context wiring break runner construction
        return None
