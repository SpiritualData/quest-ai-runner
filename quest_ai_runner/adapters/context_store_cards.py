"""Reading and writing cards: record, update, content edits, rendering, embedding export.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import heapq
import logging
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from hubofhubs.recall import card_limit as limit_mod
from hubofhubs.recall import card_origin
from hubofhubs.recall.card_store_identity import SCHEME_QUALIFIED, read_meta as read_store_meta
from hubofhubs.recall.card_content_render import (
    dedupe_content,
    normalize_content,
    render_card_content,
    tokenize,
)
from hubofhubs.recall.card_repository import card_embed_text
from hubofhubs.recall.background_pacing import Pacer
from hubofhubs.recall.scope_tags import union_scope_tags
from ..core.vector_scopes import SeedItems
from hubofhubs.recall.context_card_extraction import card_slug, managed_names, trim_content_by_recency
from .context_card_dedup import _record_summary

# How many cards an edit may be passed on through when the card it was meant for has been moved out and the card
# that stood for it was moved too.
MOVED_CARD_HOPS = 3

# How many card keys are sorted together before the next slice starts. One sort of 140,000 keys is
# about 80 ms in a single call, and the interpreter's lock is held for all of it.
RANK_SLICE = 200

_log = logging.getLogger("quest-ai-runner.context")


def card_seed_key(card_id: str, card: Dict[str, Any]) -> Tuple[int, float, int, str]:
    """Sort key ranking which cards are worth EMBEDDING first (smallest key first). No I/O.

    Managed cards (one per quest, rebuilt from the consumer's own source of truth) come first: they
    are what a quest turn is grounded in, they are few, and they carry no timestamps of their own,
    so ranking them by recency alone would put them last. Then cards that earned their place by
    being used (``usage_count``), then the rest. Within each group the card used, learned or
    changed most recently comes first (``card_origin.last_touched``), then the more used, then the id, so
    the choice is the same on every run and on every filesystem.
    """
    try:
        used = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        used = 0
    tier = 0 if card.get("managed_by") else (1 if used > 0 else 2)
    return (tier, -card_origin.last_touched(card), -used, card_id)


def cards_in_seed_order(cards: Dict[str, Dict[str, Any]]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """``(card_id, card)`` pairs, the cards most worth embedding first (see ``card_seed_key``).

    Only a key per card is computed, never an embed item, so ranking a store of any size costs a
    fraction of building items for it. The keys are sorted a slice at a time and merged lazily, so
    no single call holds the GIL for more than a slice (one sort of 140,000 keys is ~80 ms of
    stall for another thread) and a caller that stops after the best few never merges the rest.
    The work is paced (``Pacer``): it runs on a background thread beside a turn that is waiting.
    """
    pace = Pacer()
    slices: List[List[Tuple[int, float, int, str]]] = []
    current: List[Tuple[int, float, int, str]] = []
    for card_id, card in cards.items():
        current.append(card_seed_key(card_id, card))
        pace()
        if len(current) >= RANK_SLICE:
            current.sort()
            slices.append(current)
            current = []
    if current:
        current.sort()
        slices.append(current)
    return ((key[3], cards[key[3]]) for key in heapq.merge(*slices))


class CardsMixin:
    """Reading and writing cards: record, update, content edits, rendering, embedding export."""

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Upsert a card for this task and write it atomically. Never raises.

        Beyond re-pinning files (``outcome["files"]``), ``record`` can also append source-agnostic
        CONTENT: pass ``outcome["content"]`` as a list of content items (or single dicts) to append
        them to the card's ``content`` list (recency-trimmed on write). This generalizes ``record``
        so a run can accumulate notes / collection refs / conversation refs, not just files.
        """
        try:
            if self._record_is_brief_only(outcome):
                return
            card_id = self._record_card_id(task_text, outcome)
            victims = self.limit_guard.prepare(card_id)         # off the lock: which cold cards could make room
            with self.folder_lock():
                self._record_inner(task_text, outcome, victims)
            self.limit_guard.speak()                            # the notice, now that no lock is held
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Card-update API: read-modify-write a card's source-agnostic content.
    # ------------------------------------------------------------------

    def add_content(
        self, card_id: str, item: Dict[str, Any], *, scope_tags: Optional[List[str]] = None,
    ) -> bool:
        """Append ONE content item to ``card_id`` (creating the card if absent). Never raises.

        Safe read-modify-write: loads the card, normalizes + appends the item, applies the recency
        trim, and persists atomically. Returns True on a successful write, False otherwise. The item
        is normalized (type defaults to ``note``, ``ts`` coerced, ``id`` synthesized when missing),
        so a caller may pass a partial dict. An async LLM updater will use this; the API + tests are
        built now, the updater later. ``scope_tags`` (see ``core/scope_tags.py``), when given, are
        UNIONED onto the card's existing ``scope_tags`` -- never overwritten.
        """
        try:
            return self._update_card_inner(card_id, add=[item], scope_tags=scope_tags)
        except Exception:  # noqa: BLE001
            return False

    def update_content(
        self, card_id: str, item_id: str, new_item: Dict[str, Any], *,
        scope_tags: Optional[List[str]] = None,
    ) -> bool:
        """Correct/replace the content item ``item_id`` on ``card_id`` with ``new_item``. Never raises.

        Read-modify-write: the matching item is replaced in place (keeping ``item_id`` unless
        ``new_item`` supplies its own ``id``); if no item matches, ``new_item`` is appended instead.
        Returns True on a successful write. This is how a correction lands without rewriting the
        whole card. ``scope_tags`` (see ``core/scope_tags.py``), when given, are UNIONED onto the
        card's existing ``scope_tags`` -- never overwritten.
        """
        try:
            return self._update_card_inner(
                card_id, replace=[(item_id, new_item)], scope_tags=scope_tags)
        except Exception:  # noqa: BLE001
            return False

    def remove_content(self, card_id: str, item_id: str) -> bool:
        """Remove the content item ``item_id`` from ``card_id``. Never raises.

        Read-modify-write: drops the matching item and persists. Returns True on a successful write
        (including when the id was already absent and the card was simply re-saved unchanged).
        """
        try:
            return self._update_card_inner(card_id, remove=[item_id])
        except Exception:  # noqa: BLE001
            return False

    def update_card(
        self,
        card_id: str,
        *,
        add: Optional[List[Dict[str, Any]]] = None,
        replace: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        remove: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
        scope_tags: Optional[List[str]] = None,
    ) -> bool:
        """Apply a batch of edits to ``card_id`` in ONE read-modify-write. Never raises.

        ``fields`` sets embedded card fields (``name``/``description``/``summary``); ``add`` appends
        content items, ``replace`` is a list of ``(item_id, new_item)`` corrections, and ``remove`` is
        a list of item ids to drop. All are applied to the same loaded card, the recency trim runs
        once, and the card is written atomically. Returns True on success. Editing ``name``/
        ``description``/``summary`` re-fingerprints the card so the vector store re-embeds it.
        ``scope_tags`` (see ``core/scope_tags.py``), when given, are UNIONED onto the card's
        existing ``scope_tags`` -- never overwritten, so a card touched from two quests becomes
        visible to both.
        """
        try:
            return self._update_card_inner(card_id, add=add, replace=replace, remove=remove,
                                           fields=fields, scope_tags=scope_tags)
        except Exception:  # noqa: BLE001
            return False

    def write_card(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Store ``card`` whole under ``card_id`` (create or replace). True on success. Never raises.

        The write seam for a consumer that DERIVES a card from its own source of truth and owns it
        outright (``managed_by``), e.g. one card per locally synced quest, rebuilt from that
        folder's sync file (``runner/quest_folder_index.sync_quest_cards``). Learned cards go
        through ``record``/``update_card`` instead, which merge rather than replace.
        """
        try:
            if self.is_mounted_card(card_id):
                _log.warning("write_card refused: %s belongs to a mounted store (read-only)", card_id)
                return False
            if self._cache is not None:
                # The listing in hand says whether this id is new here; without one the date is not guessed.
                card_origin.stamp_created_at(card, self._cache.get(card_id))
            victims = self.limit_guard.prepare(card_id, card)    # off the lock: which cold cards could make room
            with self.folder_lock():
                self._write_card_atomic(card_id, card, victims)
            self.limit_guard.speak()                             # the notice, now that no lock is held
            return True
        except Exception:  # noqa: BLE001
            _log.debug("write_card failed for %s", card_id, exc_info=True)
            return False

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Return the RAW stored card dict for ``card_id`` (or None if absent). Never raises.

        The read seam for any consumer that needs a card's own fields (its ``keywords`` / ``name`` /
        ``summary`` / ``description`` topic terms, its content items) without going through the
        rendered-string ``render_card`` path. Reads through the persistence boundary (never a stale
        in-memory copy), so it reflects concurrent writes from other agents. Used by the cross-session
        recall path (``ClaudeConversationsAdapter``) to pull the active card's topic terms.
        """
        try:
            loaded = self._repo.read(card_id)
            return loaded if isinstance(loaded, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def find_similar_card(
        self, text: str, *, user_id: Optional[str] = None, min_score: float
    ) -> Optional[str]:
        """Return the id of an existing card whose embedding is cosine-similar to ``text`` at or above
        ``min_score``, restricted to ``user_id``'s scope, or ``None``. Never raises.

        This is the OPTIONAL semantic-dedup capability the orchestrator's post-deep card updater
        probes (by duck-typing) before creating a near-duplicate card. It DELEGATES to the persistence
        repository's own ``find_similar_card`` when present (e.g. ``QdrantCardRepository``, which
        embeds every card on write and can vector-search them) -- reusing the SAME embedding + vector
        search the card vector arm uses, never a second embedding path. When the repo has NO
        embeddings (the default ``FilesystemCardRepository``), this returns ``None``, so a keyword-only
        store cleanly degrades to create-as-before with no fuzzy/string fallback. Detected by
        duck-typing (``callable``), never an isinstance check, so any repo exposing the method
        participates.
        """
        try:
            finder = getattr(self._repo, "find_similar_card", None)
            if not callable(finder):
                return None
            if not (text or "").strip():
                return None
            result = finder(text, user_id=user_id, min_score=min_score)
            if not (isinstance(result, str) and result.strip()):
                return None
            if self._injected_repo:
                # A near-duplicate is MERGED INTO (its content edited), so on a shared repository it
                # must be one of this store's own cards, never another origin's.
                match = self._load_all().get(result)
                if not isinstance(match, dict) or not self._is_own_card(match):
                    return None
            return result
        except Exception:  # noqa: BLE001 — semantic dedup is best-effort
            return None

    def _write_card_atomic(self, card_id: str, card: Dict[str, Any], victims: Optional[List[str]] = None) -> None:
        """Persist ``card`` under ``card_id`` via the repository and mark the cache dirty.

        Shared by record() and the card-update API. Respects ``dry_run`` (writes nothing). Raises
        when the repository reports the write failed, so the public caller's try/except records the
        failure; callers above are all wrapped (never raises out of the public surface).
        """
        if self._dry_run:
            return
        if not self._store_write(card_id, card, victims):
            raise OSError(f"card repository write failed for {card_id!r}")
        self._cache_dirty = True

    def _update_card_inner(
        self,
        card_id: str,
        *,
        add: Optional[List[Dict[str, Any]]] = None,
        replace: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        remove: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
        scope_tags: Optional[List[str]] = None,
    ) -> bool:
        """Read-modify-write a card's ``content`` and embedded ``fields``: apply field edits +
        add/replace/remove, trim, persist.

        Loads the card from disk (creating a minimal one if absent), applies ``fields`` (the embedded
        ``name``/``description``/``summary``), normalizes its existing content, applies removals, then
        replacements (in place, append if the id is unknown), then additions, applies the recency
        trim, and writes atomically. Returns True on a successful write. Changing ``name``/
        ``description``/``summary`` changes the card's embedding text, so ``export_for_embedding``
        re-fingerprints it and ``VectorStore.sync()`` re-embeds it on the next sync.

        CONSUMER-MANAGED CARDS. A card MAY declare parts of itself as owned by whoever writes it
        (a consumer that derives the card from its own source of truth and rewrites it whenever that
        source changes, e.g. one card per record in the consumer's database):

          * ``managed_fields``: list of embedded field names (``name``/``description``/``summary``)
            this API must NOT edit. An edit to a managed field is dropped; the rest still applies.
          * ``managed_items``: list of content-item ids this API must NOT remove or replace.

        Additions are never blocked, so a learning updater keeps accruing notes onto a managed card
        (which is the point of putting them on the same card) while the consumer-owned digest and its
        live reference stay exactly as the consumer wrote them. A card that declares neither key
        behaves exactly as before.

        ``scope_tags`` (see ``core/scope_tags.py``), when given, are UNIONED onto the card's existing
        ``scope_tags`` -- a pure addition, so it is safe even for a managed card (nothing is ever
        removed): a card touched from a turn scoped to a new quest becomes visible to that quest too.
        """
        # A vector point id (``card:<id>``) names the card it stands for. Written under its own name
        # it would be a second card, next to the one the edit was meant for. Only this store's own
        # qualifier is dropped: another store's stays, so the read-only guard below sees the card
        # of a mounted store the edit was really meant for (it must not land on a card of ours).
        card_id = card_origin.card_id_of_item(card_id, self.own_store_id())
        if self.is_mounted_card(card_id):
            # A mounted store is read-only here: its owner keeps its cards. Never an error for the
            # caller (the post-deep updater may have been shown a mounted card), just not written.
            _log.info("card update skipped: %s belongs to a mounted store (read-only)", card_id)
            return False
        if card_origin.is_store_qualified(card_id):
            _log.info("card update skipped: %s names a card of another store that is not mounted here", card_id)
            return False
        victims = self.limit_guard.prepare(card_id)             # off the lock: which cold cards could make room
        try:
            with self.folder_lock():
                return self._update_card_locked(card_id, add=add, replace=replace, remove=remove,
                                                fields=fields, scope_tags=scope_tags, victims=victims)
        finally:
            self.limit_guard.speak()                            # the notice, now that no lock is held

    def _update_card_locked(
        self,
        card_id: str,
        *,
        add: Optional[List[Dict[str, Any]]] = None,
        replace: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        remove: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
        scope_tags: Optional[List[str]] = None,
        victims: Optional[List[str]] = None,
        hops: int = 0,
    ) -> bool:
        """The read-modify-write of ``_update_card_inner``, run while the card folder's lock is held."""
        loaded = self._repo.read(card_id)
        if not isinstance(loaded, dict):
            finder = getattr(self._own_repo, "quarantined", None)
            where = finder(card_id) if callable(finder) else None
            if where is not None:
                # A card that was moved out of the index (a tidy, a retirement, the card limit) is not
                # recreated as an empty stub carrying one note: that is how a stale writer used to push the
                # store over its limit. The edit goes to the card that stands for it now, or the card comes
                # back, or the loss is said out loud. It is never dropped quietly.
                return self._edit_moved_card(card_id, where, hops, add=add, replace=replace, remove=remove,
                                             fields=fields, scope_tags=scope_tags, victims=victims)
        card: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        card.setdefault("id", card_id)
        if scope_tags:
            card["scope_tags"] = union_scope_tags(card.get("scope_tags"), scope_tags)

        # The card's own declaration of what its WRITER owns (see the docstring). Read from the
        # loaded card, so only a card that opted in is protected and every other card is untouched.
        managed_fields = managed_names(card.get("managed_fields"))
        managed_items = managed_names(card.get("managed_items"))

        # 0) embedded field edits (name/description/summary). These are part of the embedding text,
        # so changing them re-fingerprints the card and triggers re-embedding on the next sync.
        # A field the card declares as consumer-managed is skipped: the consumer rewrites it from its
        # own source of truth, so an edit here would be overwritten on the next sync anyway (and in
        # the meantime would leave the card describing something its live reference contradicts).
        if fields:
            for _k in ("name", "description", "summary"):
                if _k in fields and fields[_k] is not None and _k not in managed_fields:
                    card[_k] = fields[_k]
        # A card with no ``summary`` is invisible to everything that reads one (the keyword index,
        # the relevance filter, the rendered header). An updater supplies ``name``/``description``,
        # so seed the summary from them the first time. Never OVERWRITES an existing summary (a
        # bootstrapped card keeps its own), so this only fills a gap.
        if not (isinstance(card.get("summary"), str) and card["summary"].strip()):
            seed = card.get("description") or card.get("name") or ""
            if isinstance(seed, str) and seed.strip():
                card["summary"] = seed.strip()

        content = normalize_content(card.get("content"))

        # 1) removals (a consumer-managed item is never removable through this API)
        if remove:
            remove_set = {r for r in remove if r and r not in managed_items}
            content = [it for it in content if it.get("id") not in remove_set]

        # 2) replacements (correct in place; append when the id is unknown)
        for item_id, new_raw in (replace or []):
            if item_id in managed_items:
                continue
            normalized = normalize_content([new_raw])
            if not normalized:
                continue
            new_item = normalized[0]
            # Keep the targeted id unless the caller explicitly supplied a different one.
            if not (isinstance(new_raw, dict) and new_raw.get("id")):
                new_item["id"] = item_id
            replaced = False
            for i, it in enumerate(content):
                if it.get("id") == item_id:
                    content[i] = new_item
                    replaced = True
                    break
            if not replaced:
                content.append(new_item)

        # 3) additions
        if add:
            content.extend(normalize_content(add))

        # 3b) collapse duplicate references (same collection id / file path / note) into one merged
        # item, keeping the existing item's stable id and the newest ts + freshest why, so re-adding
        # a reference across deep runs never bloats the card. Existing content precedes additions, so
        # an existing item keeps its id while picking up the newer ts/why from the re-add.
        content = dedupe_content(content)

        # 4) recency trim + persist
        card["content"] = trim_content_by_recency(content)
        card_origin.stamp_created_at(card, loaded if isinstance(loaded, dict) else None)
        self._write_card_atomic(card_id, card, victims)
        return not self._dry_run

    def _edit_moved_card(self, card_id: str, where: Any, hops: int, *, add: Any, replace: Any, remove: Any,
                         fields: Any, scope_tags: Any, victims: Any) -> bool:
        """An edit meant for a card that now sits in ``_quarantine/``: send it to the card that stands for it
        (recorded in the manifest of the move that took it out), bring back a card the card limit moved out
        to make room, or, when neither is possible, log the edit in full at WARNING. Caller holds the lock."""
        from hubofhubs.recall import card_store_hygiene as hygiene
        keeper = hygiene.keeper_of_moved(self._cards_dir, card_id)
        edit = dict(add=add, replace=replace, remove=remove, fields=fields, scope_tags=scope_tags)
        if keeper and keeper != card_id and hops < MOVED_CARD_HOPS and not card_origin.is_store_qualified(keeper):
            _log.info("card update for %s goes to %s, which stands for it now", card_id, keeper)
            return self._update_card_locked(keeper, victims=victims, hops=hops + 1, **edit)
        if where.parent.name == limit_mod.QUARANTINE_REASON and not self._dry_run:
            back = self._cards_dir / f"{card_id}.json"
            if not back.exists():
                try:
                    os.replace(str(where), str(back))
                except OSError:
                    back = None
                if back is not None:
                    self.folder_bump()
                    self.limit_guard.note_restored()
                    _log.info("card %s was moved out to make room and is back in the index: a run added to it",
                              card_id)
                    return self._update_card_locked(card_id, victims=victims, hops=hops + 1, **edit)
        shown = json.dumps({k: v for k, v in edit.items() if v}, default=str, ensure_ascii=False)
        _log.warning("card update NOT applied: %s was moved out of the index (it is in %s) and no card is recorded "
                     "as standing for it. The edit was: %s", card_id, where.parent, shown[:2000],
                     extra={"in_transcript": False})
        return False

    @staticmethod
    def _record_is_brief_only(outcome: Dict[str, Any]) -> bool:
        """A queued run (it carries its own ``task_id``) that pinned no file and added no content learned
        nothing to keep: its task text is a composed brief, input to the run, so no card is written for it."""
        return bool(outcome.get("task_id")) and not outcome.get("files") and not outcome.get("content")

    @staticmethod
    def _record_card_id(task_text: str, outcome: Dict[str, Any]) -> str:
        # A run on a THREAD (a recurring or autopilot task) records onto one card per thread, not
        # one per pass: each pass's composed brief differs, so keying by its text minted a new card
        # every day, and that card, keyed on the whole brief's vocabulary, was then the top match
        # for the next pass of the same thread (self-retrieval, 2026-10-06).
        own_task = str(outcome.get("task_id") or "")
        return card_slug(f"thread record {own_task}") if own_task else card_slug(task_text)

    def _record_inner(self, task_text: str, outcome: Dict[str, Any], victims: Optional[List[str]] = None) -> None:
        # Keywords come from the request's gist, never every word of a composed brief.
        from ..core.prompt_budget import decision_excerpt
        own_task = str(outcome.get("task_id") or "")
        card_id = self._record_card_id(task_text, outcome)

        # Load existing card or start fresh.
        loaded = self._repo.read(card_id)
        card: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}

        # Ensure required fields exist.
        card.setdefault("id", card_id)
        # A queued run's task text is a brief: its keywords, summary and provenance come from what the
        # run did (response, files), never from the brief.
        gist = str(outcome.get("response") or "") if own_task else task_text
        card.setdefault("keywords", sorted(tokenize(decision_excerpt(gist))))
        # NOT the raw task text: for a persona run its first 200 characters are scaffolding that
        # is identical across every run (see _record_summary).
        card.setdefault("summary", _record_summary("" if own_task else task_text, outcome,
                                                   card.get("name", "") or (card_id if own_task else "")))
        card.setdefault("conventions", [])
        card.setdefault("usage_count", 0)
        card.setdefault("last_outcome", "unknown")
        card.setdefault("provenance", {
            "created_by_task": f"run {own_task}" if own_task else task_text[:100],
            "model": "",
            "created_at": "",
            "last_verified_at": "",
        })
        if own_task:
            # Which thread this card records, so that thread's own later runs never retrieve it.
            card["provenance"]["task_id"] = own_task

        # Re-pin file fingerprints when outcome supplies a file list.
        file_paths: List[str] = outcome.get("files") or []
        if file_paths:
            existing_files: Dict[str, Dict[str, Any]] = {
                fe["path"]: fe for fe in card.get("files", []) if "path" in fe
            }
            refreshed: List[Dict[str, Any]] = []
            for fpath in file_paths:
                fp = self._fingerprint(fpath)
                entry = existing_files.get(fpath, {"path": fpath, "why": "", "symbols": []})
                entry = dict(entry)  # copy to avoid mutating the source dict
                entry["path"] = fpath
                entry["sha256"] = fp.get("sha256", "")
                entry["mtime"] = fp.get("mtime", 0.0)
                entry["git_sha"] = fp.get("git_sha", "")
                refreshed.append(entry)
            card["files"] = refreshed
        # else: keep existing file entries unchanged

        # Update usage and outcome.
        card["usage_count"] = card.get("usage_count", 0) + 1
        kind = outcome.get("kind")
        if kind in ("met", "failed", "unknown", "answer", "deep", "confirm"):
            # Normalize runner kind strings to the card schema vocabulary.
            card["last_outcome"] = kind if kind in ("met", "failed", "unknown") else {
                "answer": "met", "deep": "met", "confirm": "unknown"
            }.get(kind, "unknown")

        # Accept an optional timestamp for last_verified_at (avoids datetime.now in tests).
        ts = outcome.get("verified_at") or outcome.get("timestamp")
        if ts:
            card["provenance"]["last_verified_at"] = str(ts)

        # Generalized: append any source-agnostic CONTENT items the outcome carries (notes,
        # collection/conversation refs) to the card's content list, then recency-trim. A single
        # dict is accepted as well as a list. This keeps record()'s file-pinning intact while
        # letting a run accumulate non-file content on the same card.
        raw_content = outcome.get("content")
        if raw_content:
            if isinstance(raw_content, dict):
                raw_content = [raw_content]
            existing_content = normalize_content(card.get("content"))
            existing_content.extend(normalize_content(raw_content))
            # Collapse duplicate references (same collection id / file path / note) before trimming,
            # so re-recording the same source on a card merges instead of accumulating copies.
            existing_content = dedupe_content(existing_content)
            card["content"] = trim_content_by_recency(existing_content)

        # SCOPE TAGS (see core/scope_tags.py): a turn's outcome may carry ``scope_tags`` (derived by
        # the orchestrator from the quest(s) it is scoped to). UNION them onto the card's own
        # scope_tags rather than overwrite, so a card touched from two different quests becomes
        # visible to both -- never removes a tag a card already had, so this is safe even for a
        # consumer-managed card (managed_fields/managed_items above are about content, not tags).
        outcome_scope_tags = outcome.get("scope_tags")
        if outcome_scope_tags:
            card["scope_tags"] = union_scope_tags(card.get("scope_tags"), outcome_scope_tags)

        card_origin.stamp_created_at(card, loaded if isinstance(loaded, dict) else None)

        # Persist via the repository (skip if dry-run mode). Re-raise on failure so the outer
        # try/except in record() catches it; the cache is invalidated so assemble() sees the card.
        if not self._dry_run:
            if not self._store_write(card_id, card, victims):
                raise OSError(f"card repository write failed for {card_id!r}")
            self._cache_dirty = True

    def _render_card_content(self, card: Dict[str, Any], task_kws: Set[str]) -> List[str]:
        """Render a card's source-agnostic ``content`` items into context lines. Never raises.

        Thin wrapper over the SHARED ``render_card_content`` routine (in ``card_content_render``) so
        this keyword arm and the vector arm resolve a selected card's references identically. Passes
        this store's wired resolver registry and recency-bound limits.
        """
        return render_card_content(
            card,
            self._resolvers,
            task_kws=task_kws,
            max_refs=self._max_card_refs,
            max_ref_chars=self._max_card_ref_chars,
        )

    def render_card(self, card_id: str, *, meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Render ONE card by id for the brain's mid-loop ``{"card": <id>}`` read. Never raises.

        Reads the raw card through the persistence boundary (``CardRepository.read``) and renders its
        references FRESH with the SAME ``render_card_content`` routine the turn-start selection uses,
        so a directly-fetched card reads identically to a selected one. Returns None when the card is
        absent or has no renderable content (the brain turns None into a NAMED "no such card"
        observation). ``meta`` is accepted for interface parity and currently unused here.
        """
        try:
            card = self._repo.read(card_id)
            if not isinstance(card, dict):
                return None
            title = (card.get("name") or card.get("summary") or card_id or "").strip()
            body = "\n".join(self._render_card_content(card, set())).strip()
            if not body:
                return None
            return f"[{card_id}] {title}\n{body}" if title else body
        except Exception:  # noqa: BLE001 — a card fetch must never raise into the loop
            return None

    def _count_cards_on_disk(self) -> int:
        """Count the cards the repository holds (used for the bootstrap-meta card_count).

        Asks the repository rather than scanning the filesystem, so a non-filesystem repo reports
        the right count. Never raises.
        """
        try:
            return len(self._own_repo.load_all())
        except Exception:  # noqa: BLE001
            return 0

    def export_for_embedding(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return one item per card suitable for ``VectorStore.sync()``.

        Each item is::

            {
              "id": "card:<card_id>",
              "text": <description when present, else summary>,
              "payload": {
                "paths": [file paths],
                "symbols": [symbol names],
                "summary": <summary>,
                "kind": "bootstrap",
              },
              "fingerprint": <sha256 hash of the card's file fingerprints,
                             changes only when file content changes>,
            }

        The ``fingerprint`` is a hash of the stored sha256 values for every file
        pinned by the card. When files change the card is refreshed by
        ``bootstrap()`` and the fingerprint changes, causing ``sync()`` to
        re-embed only those cards (AUTO-UPDATE). Unchanged cards are skipped.

        ``limit`` (normally the vector arm's seed ceiling) caps how many items are BUILT. A store
        over it is ranked first by a cheap per-card key (``card_seed_key``: managed cards, then used
        ones, the most recently used or changed first) and items are built for only the best
        ``limit`` cards, so the caller no longer pays to build every card's item and then drop all
        but the first few. The result is a ``SeedItems`` whose ``offered`` is the store's card
        count. With no ``limit``, or a store within it, every card gets an item, in store order.

        Builds the in-memory card cache if it has not been loaded yet. Returns
        ``[]`` on any error. Never raises.
        """
        try:
            import hashlib as _hl
            # Ensure the store is populated before exporting. This is important when
            # export_for_embedding is called from a parallel thread (e.g. by the vector
            # arm of a HybridContextAssembler) before the keyword arm has had a chance
            # to call assemble() and trigger its own auto-bootstrap.
            self._maybe_auto_bootstrap()
            if self.inflated():
                return []     # never embed or sync an inflated store (see context_store_heal)
            # OWN cards only. A mounted store's cards are embedded by their owner; a reader that
            # exported them would embed every card once per corpus that mounts it.
            cards = self._own_cards()
            meta = read_store_meta(str(self._cards_dir))
            store_id = str(meta.get("store_id") or "")
            qualified = store_id and meta.get("vector_id_scheme") == SCHEME_QUALIFIED
            result: List[Dict[str, Any]] = []
            capped = bool(limit) and limit > 0 and len(cards) > limit
            ordered = cards_in_seed_order(cards) if capped else cards.items()
            pace = Pacer()
            for card_id, card in ordered:
                if capped and len(result) >= limit:
                    break
                pace()
                try:
                    # --- Text: the SHARED card embed-text helper (``card_embed_text``) — the topic
                    # NAME, the docstring-rich description (fall back to summary), the card's
                    # source-agnostic CONTENT (note text + each item's ``why``), then keywords — so a
                    # pure note/collection card with NO description/summary is still embeddable and the
                    # topic name is part of the embedded vector. Using the shared helper guarantees the
                    # vector arm's seed/sync text and an embedding repo's write-time text NEVER drift. ---
                    text = card_embed_text(card)
                    if not text:
                        continue

                    # --- Payload ---
                    paths: List[str] = [
                        fe.get("path", "") for fe in card.get("files", []) if fe.get("path")
                    ]
                    symbols: List[str] = []
                    for fe in card.get("files", []):
                        symbols.extend(fe.get("symbols", []))
                    # Deduplicate symbols preserving order.
                    seen_syms: Set[str] = set()
                    unique_syms: List[str] = []
                    for s in symbols:
                        if s not in seen_syms:
                            seen_syms.add(s)
                            unique_syms.append(s)

                    payload: Dict[str, Any] = {
                        "paths": paths,
                        "symbols": unique_syms[:30],
                        "summary": card.get("summary", ""),
                        "kind": "bootstrap",
                    }
                    if store_id:
                        # The point is attributed to its store, so readers that mount this store
                        # can query it by id. Not part of the fingerprint: stamping an existing
                        # point is a payload write, never a re-embedding.
                        payload["store_id"] = store_id

                    # --- Fingerprint: hash of all stored file sha256 values AND the embedded text.
                    # Including the text means the card re-embeds when EITHER its files change OR its
                    # embedded name/description/content change (the async updater rewrites those, and
                    # the vector store must re-embed on the next sync) -- not only on file changes. ---
                    fp_parts: List[str] = []
                    for fe in card.get("files", []):
                        s = fe.get("sha256") or ""
                        if s:
                            fp_parts.append(s)
                    fp_basis = "|".join(sorted(fp_parts)) + "\x00" + text
                    fingerprint = _hl.sha256(fp_basis.encode("utf-8")).hexdigest()[:16]

                    result.append({
                        "id": f"card:{store_id}:{card_id}" if qualified else f"card:{card_id}",
                        "text": text,
                        "payload": payload,
                        "fingerprint": fingerprint,
                    })
                except Exception:  # noqa: BLE001 — skip malformed card
                    continue
            return SeedItems(result, offered=len(cards)) if limit else result
        except Exception:  # noqa: BLE001
            return []

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all cards via the repository. Returns {card_id: card_dict}.

        Uses an in-memory cache to avoid re-fetching every card on every call.
        Invalidates the cache when:
        - ``_cache_dirty`` is set (after any local write via ``record()`` or
          ``bootstrap()``), OR
        - the repository's ``revision()`` change-stamp changed (external write).
        """
        current_stamp = self._repo.revision()
        moving = self.moving_ids
        need_reload = (
            self._cache is None
            or self._cache_dirty
            # While a tidy moves cards out, every move changes the stamp: keep the snapshot already
            # held (which leaves the moving cards out) instead of reloading the store after each one.
            or (current_stamp != self._cache_dir_stamp and not moving)
        )
        if not need_reload:
            return self._cache  # type: ignore[return-value]

        # One reload at a time: the keyword arm and the vector arm's seed read the same store from
        # two threads on a turn's first assemble, and both used to parse every card (the second,
        # GIL-starved, took 6.8s for 5,800 cards). The loser waits and then reuses the result.
        with self._load_lock:
            current_stamp = self._repo.revision()
            if (self._cache is not None and not self._cache_dirty
                    and (current_stamp == self._cache_dir_stamp or moving)):
                return self._cache
            self._cache_dirty = False
            self._cache_dir_stamp = current_stamp
            loaded = self._repo.load_all()
            self._cache = {cid: c for cid, c in loaded.items() if cid not in moving} if moving else loaded
            return self._cache
