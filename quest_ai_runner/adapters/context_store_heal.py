"""Self-healing at open: tidy what is provably redundant or over the limit, and refuse to seed an inflated store.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
Off unless the store was built with ``self_heal=True`` (the runner's own builder does; a unit test or a
library caller does not, so nothing here moves a card they did not ask to move). What counts as provably
redundant, and why it is safe, is ``card_store_hygiene``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from ..core.background_pacing import Pacer
from . import card_limit as limit_mod
from . import card_store_hygiene as hygiene
from . import card_store_tidy as tidy
from .card_repository import FilesystemCardRepository
from .card_store_lock import TIDY_LOCK_FILE, StoreLock
from .context_bootstrap_support import _log


class HealMixin:
    """Tidy at open, and the consistency check that keeps an inflated store from being embedded."""

    def tidy(self, notify: Optional[Callable[[str], None]] = None, quick: bool = False) -> int:
        """Move the cards that are provably copies of other cards out of the index. Never raises.

        Meant for a background thread: the scan and the moves are paced (``Pacer``), the cards being
        moved are hidden from the loader first (``moving_ids``), so the index built meanwhile never holds
        a card that is on its way out. Says what it did once: one WARNING (the terminal shows its first
        sentence as a calm line, the log holds the counts) and ``notify`` when given. Returns how many
        cards moved.

        The plan is made from the cards the store has already parsed for its turns, so a large store is
        not parsed twice. With ``quick`` (the startup tidy) nothing is planned unless a card file NAME shows
        something to tidy (a vector point id, or an id that stacks import prefixes): a clean store of any size
        pays one listing of names. A pass that has just written cards plans in full.
        """
        if not getattr(self, "heal_enabled", False) or self._dry_run:
            return 0
        if not isinstance(self._own_repo, FilesystemCardRepository):
            return 0
        if not self.tidy_lock.acquire(blocking=False):
            return 0
        process_lock = StoreLock.for_dir(str(self._cards_dir), TIDY_LOCK_FILE)
        if not process_lock.acquire(blocking=False):
            self.tidy_lock.release()
            return 0                      # another process is tidying this folder right now
        try:
            if quick and not hygiene.names_show_something(self._cards_dir):
                return 0
            pace = Pacer()
            own_store_id = self.own_store_id()
            plan = hygiene.plan_tidy(hygiene.card_files_in(self._cards_dir), pace, self._closed.is_set,
                                     own_store_id, cards=self._own_cards())
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

    def tidy_store(self, notify: Optional[Callable[[str], None]] = None) -> int:
        """The automatic tidy through the repository port, on every repository type (``card_store_tidy``). Never raises.

        Due when the store's own cards are over its card limit (every default reason: the limit itself never removes
        a card) or, on a repository other than a local folder, when an id proves a card a copy (a local folder's
        provable copies are the ``tidy`` above). When the store would still be over its limit after that plan, a
        SECOND step joins cards that pin a keeper's files under another name onto that keeper (``cards tidy
        --merge-topics``), because the owner's limit is the number to stay under. What it then does is the lane's
        ``card_tidy`` setting: ``apply`` (both steps), ``plan`` (write the plan, say once what both steps would do and
        how to apply them) or ``off``; unset, a local folder applies and any other repository plans, because its cards
        are shared team data and deleting them is the owner's call. Whatever it did or would do is said where the
        card limit's notice is: the log, ``notify`` and the next task's progress feed (``card_limit.hold_task_note``),
        so a lane run with only Quest credentials sees it. Meant for the background thread that warms the store.
        Returns how many cards it removed.
        """
        if not getattr(self, "heal_enabled", False) or self._dry_run:
            return 0
        repo = self._own_repo
        mode = tidy.resolve_mode(getattr(self, "tidy_mode", None), repo)
        if mode == "off":
            return 0
        try:
            cards = self._own_cards()
            limit = self.limit_guard.limit
            reasons, why = tidy.due(cards, limit)
            over_folders = self.over_folder_budget(cards)
            if over_folders and not reasons:
                reasons, why = tidy.DEFAULT_REASONS, "its topic cards outnumber the folders of its corpus"
            if not reasons or (tidy.is_local_folder(repo) and reasons == tidy.PROVABLE_REASONS):
                return 0
            own_store_id = self.own_store_id()
            plan = tidy.plan_store(cards, cards_dir=self._cards_dir, reasons=reasons, own_store_id=own_store_id,
                                   repo=repo)
            plan.why = why
            if (limit > 0 and len(cards) - len(plan.entries) > limit) or over_folders:
                # Still over the owner's limit after the plan, or holding more topic cards than the corpus has
                # folders (a store that is neither embedded nor synced until it is tidied): the second step joins
                # same-file topics too.
                plan = tidy.with_second_step(cards, plan, cards_dir=self._cards_dir, own_store_id=own_store_id,
                                             repo=repo)
            if not plan.entries:
                return 0
            if mode == "plan":
                self.announce_plan(plan, len(cards), notify)
                return 0
            run = self.apply_tidy(repo, plan, cards)
            if run.removed:
                self.inflation_verdict = None
                seconds = {e["key"] for e in tidy.split_steps(plan)[1]}
                message = tidy.tidied_message(
                    run, f"quest-ai-runner cards tidy --undo {run.manifest} {tidy.lane_args(repo, self._cards_dir)}",
                    joined=len(seconds & set(run.removed_keys)))
                _log.warning("%s Because %s. By reason: %s.", message, why, plan.summary())
                self.say(message, notify)
            return run.removed
        except Exception:  # noqa: BLE001
            _log.warning("context index: the card tidy stopped on an error", exc_info=True)
            return 0

    def apply_tidy(self, repo, plan: "tidy.StorePlan", cards) -> "tidy.TidyRun":
        """Carry one plan out with the cards it removes hidden from the loader meanwhile; log what did not go well."""
        self.moving_ids = frozenset(e["key"] for e in plan.entries)
        self._cache_dirty = True
        try:
            runner = tidy.RepositoryTidy(repo, self._cards_dir, pace=Pacer(), should_stop=self._closed.is_set,
                                         batch_pause=0.0 if tidy.is_local_folder(repo) else 0.2)
            run = runner.apply(plan, cards)
        finally:
            self.moving_ids = frozenset()
            self._cache_dirty = True
        if run.failed or run.problems:
            _log.warning("context index: the card tidy %s; %d check(s) failed%s; the manifest %s lists them and the "
                         "export %s undoes what was done", run.failed or "finished", len(run.failed_checks),
                         f" ({'; '.join(run.problems[:5])})" if run.problems else "", run.manifest, run.export)
        return run

    def say(self, message: str, notify: Optional[Callable[[str], None]]) -> None:
        """Give a tidy notice where the card limit gives its own: ``notify`` and the next task's progress feed."""
        if notify is not None:
            try:
                notify(message)
            except Exception:  # noqa: BLE001 -- a broken callback must not stop the notice
                _log.debug("card tidy notice callback failed", exc_info=True)
        limit_mod.hold_task_note(f"{self._cards_dir}#tidy", f"About the context store, not this task: {message}")

    def announce_plan(self, plan: "tidy.StorePlan", total: int, notify: Optional[Callable[[str], None]]) -> None:
        """Write the plan beside the cards and say once (per distinct plan) what it would do and how to apply it."""
        folder = tidy.private_dir(tidy.state_dir_for(self._cards_dir))
        target = folder / tidy.PLAN_FILE
        try:
            previous = json.loads(target.read_text(encoding="utf-8")).get("fingerprint")
        except (OSError, ValueError, AttributeError):
            previous = None
        fingerprint = plan.fingerprint()
        firsts, seconds = tidy.split_steps(plan)
        record = {"fingerprint": fingerprint, "why": plan.why, "entries": firsts, "skipped": plan.skipped,
                  "cards_after": total - len(firsts)}
        if seconds:
            record["second_step"] = {"what": "join cards that pin the same files under another name onto their keeper "
                                             "(cards tidy --merge-topics), to stay under the card limit",
                                     "entries": seconds, "cards_after": total - len(plan.entries)}
        tidy.write_private(target, json.dumps(record, indent=1, default=str) + "\n")
        repo = self._own_repo
        message = tidy.planned_message(plan, tidy.apply_command(repo, self._cards_dir), total=total,
                                       merge_hint=tidy.apply_command(repo, self._cards_dir, merge_topics=True))
        if previous == fingerprint:
            _log.info("%s", message)
            return
        _log.warning("%s The plan is in %s.", message, target)
        self.say(message, notify)

    def over_folder_budget(self, cards) -> bool:
        """Whether ``cards`` hold more topic cards than the corpus has folders (see ``inflated``). Never raises."""
        try:
            root = self._effective_root()
            if not root or not Path(root).is_dir():
                return False
            return hygiene.inflation_check(self._cards_dir, Path(root), cards).over
        except Exception:  # noqa: BLE001
            _log.debug("context index: folder budget check failed", exc_info=True)
            return False

    def inflated(self) -> bool:
        """Whether the store holds more topic cards than its corpus has folders (checked once).

        Two facts about the corpus and the store, not a quota: a topic card is about a folder, so a
        corpus cannot have more of them than folders (times the per-folder rule in force). When it
        does, something wrote cards the corpus did not call for, and the store is neither embedded nor
        synced until it is tidied (``export_for_embedding`` returns nothing). Nothing is removed for
        this reason. Only a store built with ``self_heal`` checks; others return False. Never raises.

        The corpus's folder count is a walk of the whole corpus, so it is remembered against the store's last
        completed pass (``hygiene.inflation_check``); a store that is not over it pays a count of its cards.
        """
        if not getattr(self, "heal_enabled", False) or self.inflation_verdict is not None:
            return bool(getattr(self, "inflation_verdict", False))
        self.inflation_verdict = False
        try:
            root = self._effective_root()
            if not root or not Path(root).is_dir():
                return False
            check = hygiene.inflation_check(self._cards_dir, Path(root), self._own_cards())
            self.inflation_verdict = check.over
            if check.over:
                _log.error(
                    "This context store holds %d topic cards but its corpus has only %d folders "
                    "(%d allowed per folder), so something wrote cards the corpus did not call for. "
                    "It will not be embedded or synced this session. Nothing was removed: run "
                    "'quest-ai-runner cards tidy --cards-dir %s' to see what is provably redundant.",
                    check.topic, check.folders or 0, check.per_folder, self._cards_dir)
        except Exception:  # noqa: BLE001
            _log.debug("context index: inflation check failed", exc_info=True)
        return bool(self.inflation_verdict)
