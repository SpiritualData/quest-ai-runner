"""Mounts and writer origin: which cards in a shared repository are this store's to mutate.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import contextlib
from typing import Any, ContextManager, Dict, List, Optional, Set, Tuple
from pathlib import Path

from hubofhubs.recall.card_store_lock import StoreLock

NOT_PREPARED = object()      # ``_store_write`` was not told which cold cards may move out: it looks
from .card_store_mounts import MODE_MOUNT, Mount
from .card_store_identity import ORIGIN_FIELD, read_meta as read_store_meta, writer_origins
from .context_bootstrap_support import _card_file_paths, _log


class OriginsMixin:
    """Mounts and writer origin: which cards in a shared repository are this store's to mutate."""

    # ------------------------------------------------------------------
    # Store identity and mounts (see card_store_identity / card_store_mounts)
    # ------------------------------------------------------------------

    @property
    def mounts_enabled(self) -> bool:
        """True when this store reads allowed neighbouring stores in place instead of copying."""
        return self._mount_mode == MODE_MOUNT

    def folder_lock(self) -> ContextManager[Any]:
        """The lock every process takes to change this store's card folder (a no-op where cards are not files here).

        Edits, the card limit and the tidy all work through it, so two processes never move or rewrite the
        same card at once (see ``card_store_lock``).
        """
        from .card_repository import FilesystemCardRepository
        if isinstance(self._own_repo, FilesystemCardRepository) and not self._dry_run:
            return StoreLock.for_dir(str(self._cards_dir))
        return contextlib.nullcontext()

    def folder_bump(self) -> None:
        """Record that a card joined or left this store's folder, so other processes count it again."""
        from .card_repository import FilesystemCardRepository
        if isinstance(self._own_repo, FilesystemCardRepository) and not self._dry_run:
            StoreLock.for_dir(str(self._cards_dir)).bump()

    def own_store_id(self) -> str:
        """This store's recorded ``store_id``, or "" when it has none yet (never creates one)."""
        return str(read_store_meta(str(self._cards_dir)).get("store_id") or "")

    def vector_id_scheme(self) -> str:
        return str(read_store_meta(str(self._cards_dir)).get("vector_id_scheme") or "")

    def _effective_root(self) -> Optional[str]:
        """The corpus root the cards' relative paths resolve from: the configured one, else recorded."""
        if self._repo_root is not None:
            return str(self._repo_root)
        recorded = read_store_meta(str(self._cards_dir)).get("root")
        return str(recorded) if recorded else None

    def current_mounts(self) -> List[Mount]:
        """The mounts recorded in this store's meta, re-read whenever that file changes (so a
        ``migrate-imports --apply`` run beside a live lane takes effect without a restart)."""
        try:
            stamp = (self._cards_dir / "bootstrap_meta.json").stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self._mounts_stamp:
            self._mounts = self.recorded_mounts()
            self._mounts_stamp = stamp
        return self._mounts

    def recorded_mounts(self) -> List[Mount]:
        out: List[Mount] = []
        for rec in read_store_meta(str(self._cards_dir)).get("mounts") or []:
            if isinstance(rec, dict) and rec.get("store_id") and rec.get("cards_dir"):
                out.append(Mount(store_id=str(rec["store_id"]), cards_dir=str(rec["cards_dir"]),
                                 root=str(rec.get("root") or "")))
        return out

    def live_mounts(self) -> List[Tuple[Mount, Any]]:
        """``[(mount, rebase)]`` for the mounts that are readable by this process right now."""
        if self._mounted_repo is None:
            return []
        try:
            return self._mounted_repo.live_mounts()
        except Exception:  # noqa: BLE001
            return []

    def is_mounted_card(self, card_id: str) -> bool:
        """Whether ``card_id`` names a card of a mounted store (read-only here)."""
        return self._mounted_repo is not None and self._mounted_repo.is_mounted(str(card_id))

    # ------------------------------------------------------------------
    # Writer origin: which cards in a (possibly shared) repository are THIS store's to mutate
    # ------------------------------------------------------------------

    def _origins(self) -> Tuple[str, Set[str]]:
        """``(primary, accepted)`` writer-origin ids of this store (see ``writer_origins``). Cached."""
        if self._origin_cache is None:
            from .quest_api_card_repository import runner_env_id
            self._origin_cache = writer_origins(
                str(self._cards_dir), env_id=runner_env_id(), root=self._effective_root())
        return self._origin_cache

    def origin_id(self) -> str:
        """The id stamped in the ``origin`` field of every card this store writes."""
        return self._origins()[0]

    def _is_own_card(self, card: Dict[str, Any]) -> bool:
        """Whether ``card`` is this store's to rewrite or delete during bootstrap/refresh/prune.

        * Default filesystem repository: always True (the store holds only its own cards).
        * Injected repository: True iff ``card["origin"]`` is one of this store's origin ids. A card
          with NO origin (written before origins existed) is own only if it carries this
          environment's ``runner_env_id`` AND it pins at least one file AND every pinned path is a
          relative path that resolves to an existing file under this lane's root; anything else is
          read-only here (it may belong to another lane or team member).
        """
        if not self._injected_repo:
            return True
        origin = str(card.get(ORIGIN_FIELD) or "")
        if origin:
            return origin in self._origins()[1]
        return self._legacy_card_is_own(card)

    def _legacy_card_is_own(self, card: Dict[str, Any]) -> bool:
        from .quest_api_card_repository import runner_env_id
        if str(card.get("runner_env_id") or "") != runner_env_id():
            return False
        root = self._effective_root()
        paths = _card_file_paths(card)
        if not root or not paths:
            return False
        base = Path(root).resolve()
        for rel in paths:
            try:
                target = (base / rel).resolve()
                target.relative_to(base)
            except (ValueError, OSError):
                return False
            if not target.exists():
                return False
        return True

    def _stamp_origin(self, card_id: str, card: Dict[str, Any]) -> None:
        """Stamp this store's origin on ``card`` unless it already has one.

        A card that already carries an origin keeps it (a usage-stat or content edit of another
        lane's card must not take ownership of it). An origin-less card that the repository already
        holds and that is not ours (legacy, foreign) is left as it is for the same reason.
        """
        if card.get(ORIGIN_FIELD):
            return
        if self._injected_repo:
            prior = (self._cache or {}).get(card_id)
            if isinstance(prior, dict) and not self._is_own_card(prior):
                return
        card[ORIGIN_FIELD] = self.origin_id()

    def _backfill_origins(self, own: Dict[str, Dict[str, Any]]) -> int:
        """Correct legacy cards once: stamp this store's origin on every OWN card that has none.

        Cards written before origins existed would otherwise stay origin-less forever, and in a shared
        repository an origin-less card is only conditionally ours (``_legacy_card_is_own``), so a
        later move to a shared backend would have to judge them all again. ``origin`` is not part of
        the embedded text (``card_embed_text``), so the rewrite never re-embeds. Only cards
        ``_own_cards`` already returned are touched, so a foreign card is never claimed. Skipped in
        a dry run. Returns how many were stamped."""
        if self._dry_run:
            return 0
        stamped = 0
        for cid, card in own.items():
            if card.get(ORIGIN_FIELD) or not isinstance(card, dict):
                continue
            try:
                if self._store_write(cid, card):
                    stamped += 1
            except Exception:  # noqa: BLE001 -- one unwritable card never stops the pass
                _log.warning("context index: could not stamp the writer origin on card %s", cid)
        if stamped:
            _log.info("context index: stamped this store's writer origin on %d legacy card(s)", stamped)
        return stamped

    def _store_write(self, card_id: str, card: Dict[str, Any], victims: Any = NOT_PREPARED) -> bool:
        """The ONE place this store persists a card: stamp the writer origin, ask the card limit, write.

        The limit's answer is final for a new derived card; a card already stored, and every learned
        or quest card, is let through (see ``card_limit``). The repository refuses an id that may
        never be stored (``card_origin.card_id_problem``) on its own.

        ``victims`` are the cold cards that may move out to make room, chosen by the caller BEFORE it took the
        folder's lock (``CardLimit.prepare``); a caller that holds no lock yet leaves it to this method.
        """
        self._stamp_origin(card_id, card)
        if victims is NOT_PREPARED:
            victims = self.limit_guard.prepare(card_id, card)      # off the lock: which cold cards could make room
        try:
            with self.folder_lock():
                if not self.limit_guard.admit(card_id, card, victims):
                    return False
                ok = self._repo.write(card_id, card)
                self.limit_guard.wrote(ok)
                return ok
        finally:
            self.limit_guard.speak()                            # the notice and the counters, holding no lock

    def _own_cards(self) -> Dict[str, Dict[str, Any]]:
        """The cards THIS store owns: ``_load_all()`` minus any mounted views and, with an injected
        (shared) repository, minus every card another origin wrote. Everything that rewrites or
        deletes cards works from this; reading and assembly use ``_load_all()``."""
        cards = self._load_all()
        if self._mounted_repo is not None:
            cards = {cid: c for cid, c in cards.items() if not self._mounted_repo.is_mounted(cid)}
        if self._injected_repo:
            cards = {cid: c for cid, c in cards.items() if self._is_own_card(c)}
        return cards
