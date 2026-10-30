"""The bootstrap lifecycle: stale cards, prune, refresh, nested and ancestor card imports.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from pathlib import Path

from .card_repository import FilesystemCardRepository
from .card_store_mounts import MODE_MOUNT, resolve_mounts
from hubofhubs.recall.card_content_render import MAX_CARD_REF_ITEM_CHARS as _MAX_CARD_REF_ITEM_CHARS
from hubofhubs.recall.walk import is_claude_state_file, prune_dirnames, skip_subtrees
from .walk_settings import effective_skip_dirs
from .card_store_identity import is_merged as is_merged_store, read_meta as read_store_meta
from . import card_origin
from .bootstrap_reporter import mark_bootstrap_completed
from hubofhubs.recall.background_pacing import Pacer
from hubofhubs.filemodes import match_umask
from .context_bootstrap_support import (
    _BOOTSTRAP_MAX_BYTES,
    _BOOTSTRAP_META_FILE,
    _DISCOVERED_FILE,
    _MAX_ANCESTOR_WALK_LEVELS,
    _MAX_MERGED_KEYWORDS,
    _SOURCE_EXTS,
    _TFDFIDF_VERSION,
    _card_file_paths,
    _log,
    _write_bootstrap_meta,
)
from .context_card_dedup import _first_substantive_line, _is_prompt_echo
from .context_card_ceilings import _MIN_FILES_PER_CARD, apply_card_ceilings, card_is_too_thin
from .context_folder_review import _dir_file_counts
from .context_risk_gate import folder_gate


class BootstrapMixin:
    """The bootstrap lifecycle: stale cards, prune, refresh, nested and ancestor card imports."""

    # ------------------------------------------------------------------
    # Public helper: O(1) invalidation index
    # ------------------------------------------------------------------

    def stale_cards_for(self, path: str) -> Set[str]:
        """Return card ids that pin ``path`` AND whose fingerprint for it is now stale (changed or deleted).

        The index is rebuilt on each call from the on-disk cards (bypasses the
        in-memory cache so concurrent writes from other agents are always
        reflected). Best-effort: returns an empty set on any error.
        """
        try:
            card_ids: Set[str] = set()
            for card in self._own_cards().values():
                for fe in card.get("files", []):
                    if fe.get("path") == path:
                        cur = self._current_sha(path, fe)
                        stored = fe.get("sha256", "")
                        # A pinned file that is gone (no current sha) is stale too, matching
                        # the render path in assemble; an entry with no stored sha is unknown.
                        if stored and stored != cur:
                            card_ids.add(card["id"])
            return card_ids
        except Exception:  # noqa: BLE001
            return set()

    # ------------------------------------------------------------------
    # Cold-start bootstrap
    # ------------------------------------------------------------------

    def bootstrap(
        self,
        root: Optional[str] = None,
        *,
        provider=None,
        model: Optional[str] = None,
    ) -> int:
        """Seed the cards store by walking a source tree. Never raises. Returns cards written.

        Topic cards are SEMANTIC, not structural: a single card can span files from completely
        separate directories. An LLM (the wired ``provider``) analyses the corpus file list and
        identifies the natural topics; the number of cards reflects the actual structure of the
        codebase, never a preset range. Each card captures:

        - ``id``       -- a short slug for the topic (from the LLM).
        - ``keywords`` -- 5-15 search keywords for the topic (from the LLM).
        - ``summary``  -- a one-sentence description of the topic (from the LLM).
        - ``files``    -- every file the topic references, fingerprinted (sha256/mtime/git_sha).
        - ``provenance.created_by_task`` == "bootstrap".

        Before any LLM work, corpora that already have their OWN completed bootstrap are reused
        wholesale in BOTH directions (see ``reuse_nested_cards`` on the constructor): descendant
        sub-corpora (this root is the wider one) and ancestor corpora (this root is the narrower
        one, nested inside an already-indexed wider tree) are both discovered, and their cards are
        imported with rewritten paths and a namespaced id, excluding their files from LLM
        discovery. This makes a corpus root's bootstrap free for any subtree already covered by an
        indexed QAR instance either above or below it.

        Without a ``provider`` this is a NO-OP returning 0 UNLESS reuse (either direction) produced
        cards: topic cards for genuinely new content require semantic understanding, so those
        accumulate via ``record()`` instead, but reused cards need no LLM at all.

        On success (``n > 0``) a ``bootstrap_meta.json`` sidecar is written recording the
        algorithm version + card count, so a later run can detect a stale index and re-build.

        In dry-run mode, the bootstrap runs through all the same steps but does not write
        cards to disk. Token counts are still tracked in the provider.
        """
        try:
            n = self._bootstrap_inner(root=root, provider=provider, model=model)
        except Exception:  # noqa: BLE001
            # Never silently: a bootstrap that dies here reports "0 cards" exactly like one that
            # had nothing to do, and the difference matters enormously -- the first leaves the
            # corpus permanently uncovered while looking finished. One real run identified 48
            # topic cards, threw on the way to writing them, and printed "Cards created: 0".
            _log.exception("context index: bootstrap failed before writing; no cards were saved")
            return 0
        if n > 0 and not self._dry_run:
            _write_bootstrap_meta(
                str(self._cards_dir),
                self._count_cards_on_disk(),
                feature_versions=self._completed_feature_versions(),
                root=str(Path(root).resolve() if root else self._repo_root or ""),
            )
            mark_bootstrap_completed(str(self._cards_dir))
            self.tidy()
        return n

    def _completed_feature_versions(self) -> Dict[str, int]:
        """Return the subset of per-feature versions that are complete across ALL on-disk cards.

        A feature is included only when every file entry in every card carries the current version
        for that feature.  Missing or outdated entries mean the feature migration is still in
        progress — the feature is omitted so the next startup retries it.  Never raises.
        """
        try:
            all_cards = self._own_cards()
            tfdfidf_done = all(
                fe.get("tfdfidf_v", 0) >= _TFDFIDF_VERSION
                for card in all_cards.values()
                for fe in card.get("files", [])
                if isinstance(fe, dict) and fe.get("path")
            )
            return {"tfdfidf": _TFDFIDF_VERSION} if tfdfidf_done else {}
        except Exception:  # noqa: BLE001
            return {}

    def close(self) -> None:
        """Stop this store's background indexing and refuse to start any more.

        Idempotent and safe to call from any thread. An in-flight ``bootstrap()`` / ``refresh_stale()``
        notices at its next checkpoint and returns what it has written so far (cards already written
        are kept: bootstrap is incremental and resumes next start). No new ``git hash-object``
        subprocess is spawned after this returns.

        Call it when a store's owner goes away (a consumer rebuilding its orchestrator, a CLI
        exiting, a test finishing). ``config.shutdown_background_index()`` closes every store it
        started a thread for and joins those threads, which is the usual way to reach this.
        """
        self._closed.set()

    def is_closed(self) -> bool:
        """True once ``close()`` has been called. Background indexing checks this at each checkpoint."""
        return self._closed.is_set()

    def _maybe_auto_bootstrap(self) -> None:
        """Trigger bootstrap once if auto_bootstrap is on and the store is empty. Never raises."""
        if self._bootstrap_done:
            return
        self._bootstrap_done = True  # set before any work so a failure is still final
        if not self._auto_bootstrap:
            return
        root = self._repo_root
        if root is None:
            return
        # Only bootstrap when there are no existing cards. Ask the repository (not the filesystem)
        # so a non-filesystem repo with cards already in it does NOT re-bootstrap.
        if self._own_cards():
            return
        try:
            self._bootstrap_inner(root=str(root))
        except Exception:  # noqa: BLE001
            pass

    def prune_only(self, root: Optional[str] = None) -> Tuple[int, int]:
        """Remove dead-weight cards and repair prompt-echo summaries. Returns ``(removed, healed)``.

        A PRUNE and nothing else: no model calls, no imports, no new cards. That distinction is
        the whole point of the entry point -- routing it through ``bootstrap(provider=None)``
        looked equivalent and was not, because bootstrap also pulls in every nested and ancestor
        store it can find. Used as a cleanup that way it GREW one store from 1,846 cards to 3,313
        and re-imported into another exactly the dead weight it had just removed. A cleanup that
        can add anything is not a cleanup.

        Never raises.
        """
        removed = healed = 0
        try:
            walk_root = Path(root).resolve() if root else self._repo_root
            if walk_root is None or not walk_root.is_dir():
                return (0, 0)
            skip_dirs = effective_skip_dirs(walk_root)
            walked = set(_dir_file_counts(walk_root, skip_dirs))
            walked_files: Set[str] = set()
            for dirpath, dirnames, filenames in os.walk(walk_root):
                prune_dirnames(dirnames, current=Path(dirpath), base_skip=skip_dirs)
                for fn in filenames:
                    if not is_claude_state_file(fn) and Path(fn).suffix.lower() in _SOURCE_EXTS:
                        try:
                            walked_files.add((Path(dirpath) / fn).relative_to(walk_root).as_posix())
                        except ValueError:
                            pass
            for card in list((self._own_cards() or {}).values()):
                cid = card.get("id")
                if not cid:
                    continue
                # Cap a keyword list that earlier unbounded merges let run away (see
                # _merge_card_group). A card matching every query answers none of them.
                kws = card.get("keywords") or []
                if len(kws) > _MAX_MERGED_KEYWORDS:
                    unedited = card_origin.matches_index_hash(card)
                    card["keywords"] = kws[:_MAX_MERGED_KEYWORDS]
                    if unedited:
                        card_origin.stamp_index_hash(card)      # the index's own repair is not an edit by hand
                    if not self._dry_run:
                        try:
                            self._store_write(cid, card)
                            healed += 1
                        except Exception:  # noqa: BLE001
                            pass
                summary = (card.get("summary") or "").strip()
                if summary and _is_prompt_echo(summary):
                    replacement = _first_substantive_line(summary) or (card.get("name") or "").strip()
                    if replacement and replacement != summary:
                        unedited = card_origin.matches_index_hash(card)
                        card["summary"] = replacement[:200]
                        if unedited:
                            card_origin.stamp_index_hash(card)
                        if not self._dry_run:
                            try:
                                self._store_write(cid, card)
                                healed += 1
                            except Exception:  # noqa: BLE001
                                pass
                if self._card_is_degenerate(card):
                    dead = True
                else:
                    entries = _card_file_paths(card)
                    dead = bool(entries) and all(
                        self._file_entry_is_dead(e, walked_files, walk_root, skip_dirs)
                        for e in entries)
                if dead and not self._dry_run:
                    try:
                        self._repo.delete(cid)
                        removed += 1
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            _log.exception("context index: prune failed")
        return (removed, healed)

    def refresh_stale(self, root: Optional[str] = None, *, provider=None, model: Optional[str] = None) -> int:
        """Re-index only files whose content changed since the last bootstrap.

        Existing cards are refreshed purely by a fingerprint check, so no LLM re-call is needed
        (``provider`` defaults to None and is threaded through unchanged). A card whose every
        pinned file's sha256 still matches is skipped; a card with any changed/missing file is
        rewritten with fresh fingerprints.

        Designed to be called from a background thread at startup so the context
        index stays warm without blocking the caller.  Never raises; returns the
        number of cards written (0 = everything was already up to date).
        """
        try:
            n = self._bootstrap_inner(root=root, provider=provider, model=model, skip_unchanged=True)
            if n > 0 and not self._dry_run:
                self.tidy()
            return n
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _card_is_degenerate(card: Dict[str, Any]) -> bool:
        """Whether a card carries nothing the file path does not already say.

        A healthy topic card NAMES something and describes it. The degenerate shape is a card with
        no name whose summary merely restates its own path -- ``summary: "batmanhq/api/__init__.py"``.
        It is produced when topic extraction yields nothing (a provider that is down returns no
        areas, and the pass still records the files it walked), and once written it is actively
        HARMFUL rather than merely useless: the incremental diff treats those files as COVERED, so
        a later pass with a working provider never revisits them and the corpus can never heal
        itself. On one real corpus 1,417 of 1,457 surviving cards were this shape, pinning the
        files a real bootstrap most needed to look at.

        Deliberately narrow: a card with a NAME is never degenerate however thin its summary, and
        a card with no files is never judged here at all (that is a conversation/run card, whose
        summary legitimately is not about a path).
        """
        if (card.get("name") or "").strip():
            return False
        # Nothing identifies this card: no name, and a summary that is entirely agent scaffolding
        # ("Act as X. Quest outcome: ... Scope: ...") with not one line of its own. Judged even
        # when it pins files, unlike the path-echo case below, because here there is no
        # description to salvage and no name to fall back on -- the repair pass above has already
        # tried and declined rather than invent one. The files it pins are re-carded properly by
        # the topic pass once this empty shell stops claiming them as covered.
        summary_text = (card.get("summary") or "").strip()
        if summary_text and _is_prompt_echo(summary_text) and not _first_substantive_line(summary_text):
            return True
        entries = [fe.get("path", "") if isinstance(fe, dict) else fe
                   for fe in card.get("files", [])]
        entries = [e for e in entries if e]
        if not entries:
            return False
        summary = (card.get("summary") or "").strip()
        if not summary:
            return True
        # "<path>" or "<path> -- <first line of the file>": in both cases the only thing the card
        # asserts about the content is where it lives.
        #
        # Compared as SUFFIXES in both directions, not with a plain startswith, because the same
        # file is spelled differently by stores rooted at different depths: an ancestor store
        # rooted at ``~`` pins "hq/stories/x.py" while the summary it inherited was written by a
        # store rooted at ``~/hq`` and says "stories/x.py". A prefix test silently answers False on
        # every one of those, which is exactly the population that most needs catching -- 5,001
        # such cards sat in one ancestor store and were re-imported into the corpus below it on
        # every single bootstrap, undoing each prune.
        head_tokens = summary.split(" -- ", 1)[0].strip().split()
        head = head_tokens[0] if head_tokens else ""
        if not head or ("/" not in head and "." not in head):
            return False
        return any(path == head or path.endswith("/" + head) or head.endswith("/" + path)
                   for path in entries)

    def dead_card_ids(self, cards: List[Dict[str, Any]], walked: Set[str], walk_root: Path,
                       skip_dirs: Set[str]) -> List[str]:
        """Ids of the cards whose every pinned file this corpus will never index (or that only restate
        their own path). A card with no file entries is not file-derived and is never judged."""
        dead: List[str] = []
        for card in cards:
            entries = [fe.get("path", "") if isinstance(fe, dict) else fe for fe in card.get("files", [])]
            entries = [e for e in entries if e]
            if not entries:
                continue
            if all(self._file_entry_is_dead(e, walked, walk_root, skip_dirs) for e in entries) \
                    or self._card_is_degenerate(card):
                cid = card.get("id")
                if cid:
                    dead.append(cid)
        return dead

    @staticmethod
    def _file_entry_is_dead(rel_path: str, walked: Set[str], walk_root: Path,
                            skip_dirs: Set[str]) -> bool:
        """Whether one pinned path is something this corpus will never index again.

        Deliberately NARROWER than "not in the current walk". A file can be absent from ``walked``
        for innocent reasons -- an extension outside ``_SOURCE_EXTS``, a path recorded by a run
        rather than discovered by the walk -- and deleting cards for those would throw away real
        learning. So a path counts as dead only when it is positively gone: inside a skipped
        directory, is one of Claude Code's own on-disk state files (see ``is_claude_state_file``:
        a card pinning ``.claude.json`` or a sibling isn't just excluded going forward, it is
        exactly the dead weight this pass exists to remove, and the ONLY way an existing one of
        these cards is ever cleared, since the file itself never goes missing), or absent from
        disk. Anything that still exists and is still reachable is kept, whatever the walk
        happened to collect.

        The Claude-state check runs BEFORE the ``walked`` short-circuit, unlike every other case
        here: those files are excluded from the walk itself (see the pass-1 walk in
        ``_bootstrap_inner``), so in practice ``walked`` never contains one -- but a caller that
        passes an unfiltered set (a future call site, a test) must not accidentally resurrect one.
        """
        if is_claude_state_file(Path(rel_path).name):
            return True
        if rel_path in walked:
            return False
        try:
            if any(segment in skip_dirs for segment in Path(rel_path).parts):
                return True
        except Exception:  # noqa: BLE001
            return False
        # ``Path.exists()`` is the WRONG test here: it answers False for a file that is merely
        # unreadable (permission denied) or for a malformed path, exactly as it does for one that
        # was deleted -- and this corpus really does contain directories owned by another user.
        # Deleting a card because we lacked permission to see its file would be data loss caused
        # by a missing bit. So stat() is called and only a genuine "not found" counts as dead.
        try:
            (walk_root / rel_path).stat()
            return False
        except FileNotFoundError:
            return True
        except Exception:  # noqa: BLE001 -- unreadable, malformed, or anything else: keep it
            return False

    def is_own_store(self, store_dir: Path) -> bool:
        """Whether the store in ``store_dir/.quest-context`` is THIS store.

        A store is never a source for itself, whether it is found by its path (a cards directory that
        sits inside, or above, the corpus root it indexes: ``QAR_CONTEXT_CARDS_DIR`` set away from the
        root) or by its identity (a copy of the whole folder, such as a backup snapshot). Importing
        itself added a full copy of the store to the store on every refresh. Never raises.
        """
        try:
            theirs = store_dir / ".quest-context"
            if theirs.resolve() == self._cards_dir.resolve():
                return True
            mine = str(read_store_meta(str(self._cards_dir)).get("store_id") or "")
            return bool(mine) and mine == str(read_store_meta(str(theirs)).get("store_id") or "")
        except Exception:  # noqa: BLE001
            return False

    def _discover_nested_card_dirs(self, walk_root: Path, skip_dirs: Set[str],
                                   gate: Any = None) -> List[Path]:
        """Find sub-corpora under ``walk_root`` that already have their OWN completed bootstrap.

        A directory qualifies when ``<dir>/.quest-context/bootstrap_meta.json`` exists — the
        signal a DIFFERENT (usually narrower-scoped) ``FileContextStore`` already indexed it, e.g.
        a ``product/`` corpus nested under a wider ``~/hq`` corpus. Recursion stops at each match:
        a nested store is trusted to have already reused whatever is bootstrapped beneath IT (if it
        implements the same reuse), so importing its cards transitively covers its own subtree too.
        This store's own directory is not a match (see ``is_own_store``): it is part of the corpus.
        A folder the ``gate`` (``context_risk_gate.folder_gate``) turned away is neither entered nor
        searched: a store inside it is the same dead weight arriving by another door (a backup
        snapshot carries its own store). Never raises; returns [] on any failure.
        """
        found: List[Path] = []
        try:
            walk_root_resolved = walk_root.resolve()
            cards_dir_resolved = self._cards_dir.resolve()
            for dirpath, dirnames, _filenames in os.walk(walk_root):
                current_dir = Path(dirpath).resolve()
                if current_dir == walk_root_resolved:
                    prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
                    continue
                if current_dir == cards_dir_resolved:
                    dirnames[:] = []
                    continue
                if gate is not None and gate.excludes_dir(current_dir):
                    dirnames[:] = []
                    continue
                if (current_dir / ".quest-context" / _BOOTSTRAP_META_FILE).is_file() \
                        and not is_merged_store(str(current_dir / ".quest-context")) \
                        and not self.is_own_store(current_dir):
                    found.append(current_dir)
                    dirnames[:] = []  # trust it transitively; don't look for nested-within-nested
                    continue
                prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
        except Exception:  # noqa: BLE001
            pass
        return found

    def _imported_card_unchanged(self, imported: Dict[str, Any],
                                 stored: Optional[Dict[str, Any]]) -> bool:
        """Whether a card re-imported from a nested/ancestor store already sits here unchanged.

        Every refresh re-imports every nested card. Without this check each one went through the
        fingerprint and TF-DF-IDF passes and was rewritten: 3,230 cards and 11,847 files re-read on
        every chat start, ~30s of CPU competing with the first turn. Unchanged means the same
        name/keywords/summary and path list, every stored entry at the current feature version,
        and every file's mtime still the one recorded. Never raises; any doubt re-imports.
        """
        if not stored:
            return False
        try:
            if (stored.get("name", "") != imported.get("name", "")
                    or stored.get("keywords", []) != imported.get("keywords", [])
                    or stored.get("summary", "") != imported.get("summary", "")):
                return False
            # A copy that does not yet record its origin (made before origins were kept) is
            # rewritten once, so every copy names where it came from.
            stored_prov = card_origin.provenance_of(stored)
            if any(stored_prov.get(k) != v for k, v in (imported.get("origin_fields") or {}).items()):
                return False
            entries = [fe for fe in stored.get("files", []) if isinstance(fe, dict)]
            if [fe.get("path", "") for fe in entries] != list(imported.get("files", [])):
                return False
            for fe in entries:
                if fe.get("tfdfidf_v", 0) < _TFDFIDF_VERSION:
                    return False
                p = Path(fe.get("path", ""))
                if not p.is_absolute() and self._repo_root is not None:
                    p = self._repo_root / p
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime is None:
                    # Unreadable/gone now: unchanged only if it was already recorded that way.
                    if fe.get("sha256"):
                        return False
                elif not fe.get("mtime") or float(fe["mtime"]) != mtime:
                    return False
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _card_imported_from(card: Dict[str, Any]) -> str:
        """Where a card was imported from (provenance ``imported_from``), or "" for an original."""
        prov = card.get("provenance")
        return str(prov.get("imported_from") or "") if isinstance(prov, dict) else ""

    def retire_copy(self, card_id: str, survivor_id: str = "") -> bool:
        """Take a copy that its source store no longer offers out of the index. True when THIS call moved it.

        Moved, not deleted, where the repository can (the filesystem one keeps it under
        ``_quarantine/superseded-import/``). A copy is redundant by construction, but anything added to it
        is not: a copy that carries notes, usage or quest tags moves only after all of that is joined
        onto ``survivor_id``, the card that stands for it now (``card_store_hygiene.fold_card``). With no
        survivor, or when it cannot be joined without loss, the copy stays where it is.
        """
        from . import card_store_hygiene as hygiene
        repo = getattr(self, "_own_repo", self._repo)
        quarantine = getattr(repo, "quarantine", None)
        with self.folder_lock():
            card = repo.read(card_id)
            if not isinstance(card, dict):
                return False                        # somebody else moved it already
            why = card_origin.protection(card)
            joined: Dict[str, Any] = {}
            if why:
                survivor_path = self._cards_dir / f"{survivor_id}.json" if survivor_id else None
                failed = ""
                if survivor_path is None or not survivor_path.is_file() or not callable(quarantine):
                    failed = "no card it can join stands for it"
                else:
                    failed, joined = hygiene.join_onto(self._cards_dir, card, survivor_path)
                if failed:
                    _log.info("context index: copy %s stays: %s, and %s", card_id, why, failed)
                    return False
            if callable(quarantine):
                moved = bool(quarantine(card_id, "superseded-import"))
                if moved:
                    self.folder_bump()
                    find = getattr(repo, "quarantined", None)
                    placed = find(card_id) if callable(find) else None
                    if placed is not None:
                        hygiene.record_retirement(self._cards_dir, placed, card_id, survivor_id, joined)
                return moved
            return bool(self._repo.delete(card_id))

    def reconcile_copies(self) -> int:
        """After a pass: retire a copy that pins exactly the files another card of the store pins.

        The imports were chosen against the cards as they stood at the start of the pass, and the
        pass can change them (a stale card regenerated over the files a copy also covers). Two cards
        pinning the same paths are one card; the store's own wins, and between copies the one from
        the more deeply rooted store (``card_origin.choose_imports``). Returns how many were retired.
        """
        try:
            self._cache_dirty = True
            cards = list(self._own_cards().values())
            copies = [c for c in cards if card_origin.is_copy(c) and c.get("id")]
            if not copies:
                return 0
            offered = [{"id": c["id"], "files": sorted(card_origin.file_set(c)),
                        "origin_fields": {k: v for k, v in card_origin.provenance_of(c).items()
                                          if k.startswith("origin_")},
                        "hops": 0, "protected": bool(card_origin.protection(c))} for c in copies]
            keep = {item["id"] for item in card_origin.choose_imports(offered, cards)}
            standing: Dict[frozenset, str] = {}      # the card that stands for each file set
            for c in sorted(cards, key=lambda c: (card_origin.is_copy(c), str(c.get("id")))):
                if c.get("id") and (not card_origin.is_copy(c) or c["id"] in keep):
                    standing.setdefault(card_origin.file_set(c), c["id"])
            retired = 0
            for item in offered:
                if item["id"] not in keep and self.retire_copy(
                        item["id"], standing.get(frozenset(item["files"]), "")):
                    retired += 1
            if retired:
                self._cache_dirty = True
                _log.info("context index: %d copied card(s) pinned the same files as another card; "
                          "moved out of the index", retired)
            return retired
        except Exception:  # noqa: BLE001
            return 0

    def own_refs_of_store(self) -> Set[str]:
        """Every identity this store's own cards may be attributed to (see ``card_origin``)."""
        refs = {card_origin.path_ref(str(self._cards_dir))}
        mine = str(read_store_meta(str(self._cards_dir)).get("store_id") or "")
        if mine:
            refs.add(mine)
        try:
            refs |= set(self._origins()[1])
        except Exception:  # noqa: BLE001
            pass
        return refs

    def offered_import(self, card: Dict[str, Any], files: List[str], source_ref: str, source_root: Path,
                        source_rel: str, walk_root: Path, own_refs: Set[str]) -> Optional[Dict[str, Any]]:
        """One import offered by a source store's ``card``, or None when it must not be imported.

        The copy is identified by its ORIGIN, not by the store it was read from: the id is
        ``card_origin.import_id(<origin root, relative to this root>, <id in the authoring store>)``,
        so the same card reached by two routes is one id and an id never grows by hop. A card whose
        origin is this store is never imported, whichever way it came back, whatever happened to its
        provenance on the way. Judged on the SOURCE card, before its paths are rewritten.
        """
        # Never import a card that carries nothing but its own path. Reuse is a shortcut past LLM
        # work, not a licence to spread another store's dead weight: an ancestor store full of
        # these re-seeded the corpus below it on every bootstrap, silently undoing each prune.
        if self._card_is_degenerate(card):
            return None
        origin = card_origin.exported_origin(card, source_ref, source_root)
        if origin is None or origin.store in own_refs:
            return None
        origin_root = card_origin.origin_relative_to(origin, source_root, walk_root)
        if origin_root == ".":
            return None
        return {
            "id": card_origin.import_id(origin_root, origin.id),
            "name": card.get("name", ""),
            "keywords": card.get("keywords", []),
            "summary": card.get("summary", ""),
            "description": card.get("description") or card.get("summary", ""),
            "files": files,
            "imported_from": source_rel,
            "origin_fields": card_origin.origin_fields(origin.store, origin.id, origin_root),
            "hops": 0 if origin.root in ("", ".") else 1,
        }

    def _import_nested_cards(self, nested_root: Path, walk_root: Path) -> List[Dict[str, Any]]:
        """Reuse a nested corpus's already-bootstrapped cards instead of re-discovering them.

        Pure filesystem reuse: reads the nested store's on-disk cards directly (no LLM call), then
        rewrites each file path from nested-root-relative to THIS store's walk-root-relative and
        names the copy after its origin (see ``offered_import``). Each returned dict carries
        ``imported_from`` (the nested root, relative to ``walk_root``) and its ``origin``, so
        ``_bootstrap_inner`` can write both into the card's provenance and prune it later if the
        nested store stops offering it. Never raises; returns [] on failure.
        """
        try:
            rel_root = nested_root.relative_to(walk_root)
        except ValueError:
            return []
        prefix = rel_root.as_posix()
        try:
            nested_cards = FilesystemCardRepository(str(nested_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
        source_ref = card_origin.store_ref(str(nested_root / ".quest-context"))
        own_refs = self.own_refs_of_store()
        imported: List[Dict[str, Any]] = []
        for card in nested_cards.values():
            try:
                files = [
                    (rel_root / fe["path"]).as_posix()
                    for fe in card.get("files", [])
                    if isinstance(fe, dict) and fe.get("path")
                ]
                if not files:
                    continue
                offered = self.offered_import(card, files, source_ref, nested_root, prefix, walk_root, own_refs)
                if offered is not None:
                    imported.append(offered)
            except Exception:  # noqa: BLE001
                continue
        return imported

    def _discover_ancestor_card_dir(self, walk_root: Path, gate: Any = None) -> Optional[Path]:
        """Find the nearest ANCESTOR of ``walk_root`` that already has its own completed bootstrap.

        Mirror image of ``_discover_nested_card_dirs``: that one walks DOWN into ``walk_root``'s
        descendants; this one walks UP through its parents. Covers the case where THIS store's
        corpus root is a narrower subfolder of an already-indexed wider corpus (e.g. bootstrapping
        a dissertation folder several levels below ``~/hq`` when ``~/hq`` itself already has a
        completed ``.quest-context`` covering it) -- without this, a narrower root can never see an
        ancestor's index, since an ancestor is by definition not inside ``walk_root``.

        Walks up from ``walk_root``'s parent, checking each ancestor for
        ``<ancestor>/.quest-context/bootstrap_meta.json``, and returns the FIRST (nearest) match --
        trusting it transitively, same as the downward case trusts a nested match to have already
        reused whatever is beneath IT. This store's own directory is not a match (``is_own_store``).
        Bounded by ``_MAX_ANCESTOR_WALK_LEVELS`` and stops at the filesystem root either way. Never
        raises; returns ``None`` on any failure or no match.
        """
        try:
            current = walk_root.resolve().parent
            for _ in range(_MAX_ANCESTOR_WALK_LEVELS):
                if (current / ".quest-context" / _BOOTSTRAP_META_FILE).is_file() \
                        and not is_merged_store(str(current / ".quest-context")) \
                        and not self.is_own_store(current) \
                        and not (gate is not None and gate.ancestor_blocked(current)):
                    return current
                parent = current.parent
                if parent == current:  # reached filesystem root
                    break
                current = parent
        except Exception:  # noqa: BLE001
            pass
        return None

    def _import_ancestor_cards(self, ancestor_root: Path, walk_root: Path,
                               gate: Any = None) -> List[Dict[str, Any]]:
        """Reuse an ANCESTOR corpus's already-bootstrapped cards, filtered down to ``walk_root``.

        Mirror image of ``_import_nested_cards`` for the upward direction. An ancestor's cards can
        reference files anywhere across its much wider tree, most of which are irrelevant to this
        narrower root, so a card is only imported when at least one of its files falls under
        ``walk_root`` -- and when imported, its ``files`` list is TRIMMED to just that in-scope
        subset (paths rewritten from ancestor-root-relative to walk-root-relative, the reverse
        rewrite direction from the nested/downward case) so a file the ancestor's card references
        outside this narrower corpus is never treated as "covered" here.

        ``imported_from`` is the ancestor's path relative to ``walk_root`` (e.g. ``".."`` or
        ``"../.."``), so it round-trips through the same provenance/pruning logic in
        ``_bootstrap_inner`` that the downward case already uses. Never raises; returns [] on any
        failure.
        """
        try:
            walk_root_resolved = walk_root.resolve()
            ancestor_resolved = ancestor_root.resolve()
            depth = len(walk_root_resolved.relative_to(ancestor_resolved).parts)
            if depth < 1:
                return []
        except Exception:  # noqa: BLE001
            return []
        imported_from = "/".join([".."] * depth)
        try:
            ancestor_cards = FilesystemCardRepository(str(ancestor_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
        source_ref = card_origin.store_ref(str(ancestor_root / ".quest-context"))
        own_refs = self.own_refs_of_store()
        imported: List[Dict[str, Any]] = []
        for card in ancestor_cards.values():
            try:
                files: List[str] = []
                for fe in card.get("files", []):
                    if not isinstance(fe, dict) or not fe.get("path"):
                        continue
                    try:
                        abs_path = (ancestor_root / fe["path"]).resolve()
                        rel = abs_path.relative_to(walk_root_resolved)
                    except ValueError:
                        continue  # this file is outside walk_root; drop it, keep the rest of the card
                    # A file inside a folder the gate turned away is not this corpus's to cover,
                    # whichever store offers it.
                    if gate is not None and gate.excludes_rel(rel.parent.as_posix()):
                        continue
                    files.append(rel.as_posix())
                if not files:
                    continue
                offered = self.offered_import(card, files, source_ref, ancestor_root, imported_from, walk_root, own_refs)
                if offered is not None:
                    imported.append(offered)
            except Exception:  # noqa: BLE001
                continue
        return imported

    def _bootstrap_inner(
        self,
        root: Optional[str] = None,
        *,
        provider=None,
        model: Optional[str] = None,
        max_files: Optional[int] = None,
        max_cards: Optional[int] = None,
        skip_unchanged: bool = False,
    ) -> int:
        """Index this corpus; in ``mount`` mode, first settle which neighbouring stores it MOUNTS.

        Mounting is decided here, once per pass, and then the indexing walk runs with every mounted
        store's subtree skipped outright (no walk, no cards, no staleness check, no embedding: its
        owner's lane keeps it fresh). A store BELOW this corpus that cannot be mounted (not readable
        by this process, no identity yet) is skipped just the same and logged: the reader has no
        cards for it and does NOT index those files itself, which would recreate the duplicate. A
        store ABOVE this corpus that this process cannot read is invisible to it and ignored: this
        corpus indexes its own root as usual. ``copy`` mode (the default) is the legacy behaviour,
        unchanged.
        """
        if self._closed.is_set():
            return 0
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0
        self.adopt_moved_root(walk_root)
        built_for = self.built_for_another_root(walk_root)
        if built_for:
            _log.error(
                "context index: not indexing %s: this card store was built for the corpus root %s, "
                "and its cards name files relative to that root. Point the lane at its own cards "
                "folder, or move the store with `quest-ai-runner context move-root`, or merge it "
                "into the wider store with `quest-ai-runner context merge-into`.", walk_root, built_for)
            return 0
        if self._mount_mode != MODE_MOUNT:
            return self._bootstrap_walk(root, provider=provider, model=model, max_files=max_files,
                                        max_cards=max_cards, skip_unchanged=skip_unchanged)
        # The gate is settled FIRST: a store inside a folder it turns away is neither claimed nor
        # mounted, and the walk below is handed the very same decision.
        skip_dirs = effective_skip_dirs(walk_root)
        gate = folder_gate(walk_root, skip_dirs, provider, model, self._cards_dir)
        outcome = resolve_mounts(str(self._cards_dir), walk_root, skip_dirs, gate=gate)
        if not self._dry_run and outcome.mounts and not read_store_meta(str(self._cards_dir)):
            # A corpus that mounts stores is itself a store with an identity, even before it has a
            # card of its own: record it, so its mounts (and its own cards, later) have a home.
            _write_bootstrap_meta(str(self._cards_dir), 0, root=str(walk_root))
        if not self._dry_run and read_store_meta(str(self._cards_dir)):
            from .card_store_identity import update_identity
            update_identity(str(self._cards_dir),
                            mounts=[m.as_record() for m in outcome.mounts])
        self._mounts = list(outcome.mounts)
        self._mounts_stamp = "unset"
        if outcome.mounts:
            _log.info("context index: mounting %d store(s) read-only and in place; their subtrees "
                      "are not indexed here", len(outcome.mounts))
        with skip_subtrees(outcome.claimed_roots):
            return self._bootstrap_walk(root, provider=provider, model=model, max_files=max_files,
                                        max_cards=max_cards, skip_unchanged=skip_unchanged,
                                        gate=gate)

    def adopt_moved_root(self, walk_root: Path) -> bool:
        """Record ``walk_root`` as the store's root when the corpus folder was renamed or moved. True when it did.

        A card names its files relative to the root, so a corpus renamed together with the store inside
        it (``<root>/.quest-context``) still points at the right files: only the recorded root is stale,
        and refusing to index until someone runs ``move-root`` stopped a renamed corpus for good. The
        recorded root is replaced (and kept in the store's history, as ``move-root`` does) when it no
        longer exists, or when this store sits directly inside ``walk_root``. A store kept elsewhere that
        was built for a root that still exists is NOT adopted: that is the shared or widened store the
        refusal is for. Never raises.
        """
        try:
            recorded = str(read_store_meta(str(self._cards_dir)).get("root") or "")
            if not recorded or self._dry_run or os.path.realpath(recorded) == os.path.realpath(str(walk_root)):
                return False
            inside = os.path.realpath(str(self._cards_dir)) == os.path.realpath(str(walk_root / ".quest-context"))
            if os.path.isdir(recorded) and not inside:
                return False
            from .card_store_identity import apply_move_root
            apply_move_root(str(self._cards_dir), str(walk_root), force=True)
            _log.info("context index: the corpus folder moved from %s to %s; its card store now records the new "
                      "root (the cards name files relative to it, so nothing else changes)", recorded, walk_root)
            return True
        except Exception:  # noqa: BLE001
            return False

    def built_for_another_root(self, walk_root: Path) -> str:
        """The corpus root this store was built for when it is not ``walk_root``, else "".

        A card names its files relative to the root the store was built for. Run under another root
        (two lanes sharing one cards folder, a lane whose root was widened without moving the store),
        every card points at the wrong place: the other lane's cards look dead and are pruned, and
        each lane re-cards what the other just lost. A store belongs to one root; changing it is what
        ``move-root`` and ``merge-into`` are for. Never raises.
        """
        try:
            recorded = str(read_store_meta(str(self._cards_dir)).get("root") or "")
            if recorded and os.path.realpath(recorded) != os.path.realpath(str(walk_root)):
                return recorded
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _gate_new_cards(
        self,
        topic_cards: List[Dict[str, Any]],
        existing_cards: List[Dict[str, Any]],
        *,
        max_cards: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Apply the thin-card rule and the card ceilings to what this pass would write.

        Both gates apply to GENUINELY NEW cards only. A card whose id is already in the store is a
        refresh: refusing it would leave the store stale without making it any smaller, and the
        cards that should never have existed are removed by the maintenance tool
        (``scripts/quarantine_cards.py``), not by quietly declining to update them.
        """
        existing_ids = {c.get("id") for c in existing_cards if c.get("id")}
        covered_elsewhere: Set[str] = set()
        for card in existing_cards:
            if card.get("id") in {tc.get("id") for tc in topic_cards}:
                continue
            covered_elsewhere.update(_card_file_paths(card))
        multi_file_new = [tc for tc in topic_cards
                          if len(_card_file_paths(tc)) >= _MIN_FILES_PER_CARD]
        for card in multi_file_new:
            covered_elsewhere.update(_card_file_paths(card))

        kept: List[Dict[str, Any]] = []
        thin: Dict[str, int] = {}
        for card in topic_cards:
            if card.get("id") in existing_ids:
                kept.append(card)
                continue
            reason = card_is_too_thin(card, covered_elsewhere)
            if reason:
                thin[reason] = thin.get(reason, 0) + 1
                continue
            kept.append(card)
        if thin:
            # Expected behavior: single files and generated data files don't get cards.
            # This is normal filtering, not an issue. Suppress the log noise.
            pass

        return apply_card_ceilings(kept, existing_cards, max_cards=max_cards)

    def _read_discovered(self) -> Dict[str, float]:
        """The discovered-files record (see ``_DISCOVERED_FILE``). Never raises."""
        try:
            data = json.loads((self._cards_dir / _DISCOVERED_FILE).read_text(encoding="utf-8"))
            files = data.get("files") if isinstance(data, dict) else None
            return {str(k): float(v) for k, v in files.items()} if isinstance(files, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _discovered_unchanged(self, rel: str, discovered: Dict[str, float]) -> bool:
        seen = discovered.get(rel)
        if seen is None:
            return False
        try:
            p = Path(rel)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / rel
            return p.stat().st_mtime == seen
        except OSError:
            return False

    def _record_discovered(self, paths: List[str], previous: Dict[str, float],
                           walked: Set[str]) -> None:
        """Add ``paths`` (with current mtimes) to the record; drop files no longer walked."""
        record = {k: v for k, v in previous.items() if k in walked}
        for rel in paths:
            try:
                p = Path(rel)
                if not p.is_absolute() and self._repo_root is not None:
                    p = self._repo_root / rel
                record[rel] = p.stat().st_mtime
            except OSError:
                continue
        try:
            target = self._cards_dir / _DISCOVERED_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_disc_",
                                                suffix=".json")
            match_umask(tmp_fd)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump({"v": 1, "files": record}, fh)
                os.replace(tmp_path, str(target))
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            _log.debug("context index: could not write %s", _DISCOVERED_FILE, exc_info=True)

    def prewarm(self, notify: Optional[Callable[[str], None]] = None) -> None:
        """Load the cards and build the scoring index ahead of the first turn. Never raises.

        A cold first turn otherwise pays for parsing every card and weighting every term (~7s on
        a 5,800-card store) inside its turn-start context budget. Meant to run in a background
        thread while the session waits for its first message. A store already at or above its card
        limit says so here, once (see ``card_limit``); nothing is removed to fit the limit.
        """
        try:
            self.tidy(notify, quick=True)   # first: so the index built below never holds a card on its way out
            self.tidy_store(notify)         # any repository: over its limit, or copies proven by their ids
            cards = self._load_all()
            self.limit_guard.check_at_open(notify)
            self.inflated()
            if cards:
                self._scoring_index(cards, Pacer())
        except Exception:  # noqa: BLE001
            _log.debug("context index: prewarm failed", exc_info=True)

    def _current_sha(self, path: str, stored: Optional[Dict[str, Any]] = None) -> str:
        """The file's current sha256, for a freshness comparison only. Never raises.

        Cheap on the common path: when the stored entry's mtime still matches the file on disk,
        the stored sha is returned without reading the file. Never spawns ``git``. The read path
        (``assemble``) checks every file pinned by every top card, which on a large store is tens
        of thousands of files per turn; a full read plus a ``git hash-object`` process for each one
        cost 17s+ and pushed turn-start assembly past its timeout.
        """
        try:
            p = Path(path)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / path
            st = p.stat()
        except OSError:
            return ""
        if stored:
            stored_sha = stored.get("sha256", "")
            stored_mtime = stored.get("mtime")
            if stored_sha and stored_mtime and float(stored_mtime) == st.st_mtime:
                return stored_sha
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    def _fingerprint(self, path: str) -> Dict[str, Any]:
        """Compute current sha256 + mtime + (optional) git_sha for a file path.

        Returns a dict with the fields that exist; missing/unreadable fields are omitted
        or set to empty string. Never raises.
        """
        result: Dict[str, Any] = {"sha256": "", "mtime": 0.0, "git_sha": ""}
        try:
            # Paths pinned from a run are relative to the corpus/repo root (that is what the
            # RetrievalAdapter and deep runner work in), NOT the process cwd. Resolve a relative
            # path against repo_root so freshness reads the real file rather than missing it.
            p = Path(path)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / path
            if not p.exists() or not p.is_file():
                return result
            stat = p.stat()
            result["mtime"] = stat.st_mtime
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            result["sha256"] = h.hexdigest()
        except Exception:  # noqa: BLE001
            pass

        # Git blob SHA: best-effort, optional, fully wrapped. Skipped once the store is closed: this
        # is the one call in the index that leaves the process (``git hash-object``), so a background
        # pass that outlived its owner must not still be spawning it.
        if self._repo_root is not None and result["sha256"] and not self._closed.is_set():
            try:
                proc = subprocess.run(
                    ["git", "-C", str(self._repo_root), "hash-object", path],
                    capture_output=True, text=True, timeout=5,
                )
                git_sha = proc.stdout.strip()
                if git_sha:
                    result["git_sha"] = git_sha
            except Exception:  # noqa: BLE001 -- git unavailable, no repo, any error: skip
                pass

        return result

    def _read_file_fresh(self, path: str, max_chars: int = _MAX_CARD_REF_ITEM_CHARS) -> str:
        """Read a file's CURRENT content fresh, capped at ``max_chars``. Never raises; "" on failure.

        This is the fresh-read path the built-in ``file`` ReferenceResolver uses: a ``file`` content
        item is never a stale snapshot, it re-reads the live file every time the card is used.
        Resolves a relative path against ``repo_root`` (the same convention as ``_fingerprint``), so
        a path pinned by a run reads the real file regardless of the process cwd. Returns the head of
        the file (up to ``max_chars``), with a trailing ellipsis when truncated.
        """
        try:
            p = Path(path)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / path
            if not p.exists() or not p.is_file():
                return ""
            try:
                if p.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                    # Big file: read only the head we need rather than the whole thing.
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(max_chars + 1)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            if len(text) > max_chars:
                text = text[: max_chars - 1].rstrip() + "…"
            return text
        except Exception:  # noqa: BLE001
            return ""
