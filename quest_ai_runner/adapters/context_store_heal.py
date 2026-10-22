"""Self-healing at open: move provably redundant cards out, and refuse to seed an inflated store.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
Off unless the store was built with ``self_heal=True`` (the runner's own builder does; a unit test or a
library caller does not, so nothing here moves a card they did not ask to move). What counts as provably
redundant, and why it is safe, is ``card_store_hygiene``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..core.background_pacing import Pacer
from . import card_store_hygiene as hygiene
from .card_repository import FilesystemCardRepository
from .card_store_lock import TIDY_LOCK_FILE, StoreLock
from .context_bootstrap_support import _log


class HealMixin:
    """Tidy at open, and the consistency check that keeps an inflated store from being embedded."""

    def tidy(self, notify: Optional[Callable[[str], None]] = None) -> int:
        """Move the cards that are provably copies of other cards out of the index. Never raises.

        Meant for a background thread: the scan and the moves are paced (``Pacer``), the cards being
        moved are hidden from the loader first (``moving_ids``), so the index built meanwhile never holds
        a card that is on its way out. Says what it did once: one WARNING (the terminal shows its first
        sentence as a calm line, the log holds the counts) and ``notify`` when given. Returns how many
        cards moved.
        """
        if not getattr(self, "heal_enabled", False) or self._dry_run:
            return 0
        if not isinstance(self._own_repo, FilesystemCardRepository):
            return 0
        if not self.tidy_lock.acquire(blocking=False):
            return 0
        process_lock = StoreLock.for_dir(str(self._cards_dir), TIDY_LOCK_FILE)
        if not process_lock.acquire(blocking=False):
            return 0                      # another process is tidying this folder right now
        try:
            pace = Pacer()
            own_store_id = self.own_store_id()
            plan = hygiene.plan_tidy(hygiene.card_files_in(self._cards_dir), pace, self._closed.is_set,
                                     own_store_id)
            if plan.kept:
                _log.info("context index: tidy left %s", "; ".join(
                    f"{count:,} ({why})" for why, count in sorted(plan.kept.items())))
            if not plan.moves:
                return 0
            self.moving_ids = frozenset(move.card_id for move in plan.moves)
            self._cache_dirty = True
            try:
                result = hygiene.apply_tidy(self._cards_dir, plan, pace, self._closed.is_set, own_store_id)
            finally:
                self.moving_ids = frozenset()
                self._cache_dirty = True
            if result.moved:
                self.inflation_verdict = None     # the store is smaller now: look again, never trust the old verdict
                reasons = ", ".join(f"{name} {n:,}" for name, n in sorted(result.by_reason.items()))
                _log.warning("%s Moved, by reason: %s.%s", hygiene.tidy_message(result), reasons,
                             f" Manifest: {result.manifest}." if result.manifest else "")
                if notify is not None:
                    notify(hygiene.tidy_message(result))
            if result.failed:
                _log.warning("context index: %d cards could not be moved and stay where they are", result.failed)
            if result.left:
                _log.info("context index: tidy left %s", "; ".join(
                    f"{count:,} ({why})" for why, count in sorted(result.left.items())))
            return result.moved
        except Exception:  # noqa: BLE001
            _log.warning("context index: the tidy stopped on an error; cards already moved are listed in the "
                         "manifest beside them and nothing was deleted", exc_info=True)
            return 0
        finally:
            process_lock.release()
            self.tidy_lock.release()

    def inflated(self) -> bool:
        """Whether the store holds more topic cards than its corpus has folders (checked once).

        Two facts about the corpus and the store, not a quota: a topic card is about a folder, so a
        corpus cannot have more of them than folders (times the per-folder rule in force). When it
        does, something wrote cards the corpus did not call for, and the store is neither embedded nor
        synced until it is tidied (``export_for_embedding`` returns nothing). Nothing is removed for
        this reason. Only a store built with ``self_heal`` checks; others return False. Never raises.
        """
        if not getattr(self, "heal_enabled", False) or self.inflation_verdict is not None:
            return bool(getattr(self, "inflation_verdict", False))
        self.inflation_verdict = False
        try:
            root = self._effective_root()
            if not root or not Path(root).is_dir():
                return False
            health = hygiene.store_health(self._cards_dir, root=Path(root), cards=self._own_cards())
            self.inflation_verdict = health.topic_over_folders
            if self.inflation_verdict:
                _log.error(
                    "This context store holds %d topic cards but its corpus has only %d folders "
                    "(%d allowed per folder), so something wrote cards the corpus did not call for. "
                    "It will not be embedded or synced this session. Nothing was removed: run "
                    "'quest-ai-runner cards tidy --cards-dir %s' to see what is provably redundant.",
                    health.topic, health.folders or 0, health.per_folder, self._cards_dir)
        except Exception:  # noqa: BLE001
            _log.debug("context index: inflation check failed", exc_info=True)
        return bool(self.inflation_verdict)
