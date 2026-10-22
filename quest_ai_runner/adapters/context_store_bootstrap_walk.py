"""The bootstrap walk: scanning the corpus and proposing cards.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import Any, Dict, List, Optional, Set
from pathlib import Path

from . import card_origin
from .card_store_mounts import MODE_MOUNT
from ..core.background_pacing import Pacer
from .card_content_render import tokenize as _tokenize
from ._walk import effective_skip_dirs, is_claude_state_file, prune_dirnames
from .context_bootstrap_support import (
    _BOOTSTRAP_MAX_BYTES,
    _SOURCE_EXTS,
    _TFDFIDF_VERSION,
    _bootstrap_max_cards,
    _bootstrap_max_files,
    _card_file_paths,
    _log,
    _run_parallel,
)
from .context_card_extraction import _extract_topic_cards
from .context_folder_review import (
    _is_excluded_folder,
    _prune_dead_cards,
)
from .context_risk_gate import FolderGate, folder_gate
from .context_card_ceilings import folders_for_room
from .context_card_dedup import _first_substantive_line, _is_prompt_echo
from .context_topic_cards import _llm_topic_cards


class BootstrapWalkMixin:
    """The bootstrap walk: scanning the corpus and proposing cards."""

    def _bootstrap_walk(
        self,
        root: Optional[str] = None,
        *,
        provider=None,
        model: Optional[str] = None,
        max_files: Optional[int] = None,
        max_cards: Optional[int] = None,
        skip_unchanged: bool = False,
        gate: Optional[FolderGate] = None,
    ) -> int:
        """Actual bootstrap logic. May raise; callers wrap in try/except.

        ``gate`` is the folder decision for this pass (see ``context_risk_gate.folder_gate``); the
        caller that already settled mounts passes the one it used, otherwise it is taken here.

        Topic cards are semantic: the LLM (``provider``) decides what the topics are and which
        files belong to each, so a card can span unrelated directories. The pass is INCREMENTAL:

          1. Walk the corpus (with the existing skip logic) into a flat list of qualifying
             source file paths. No symbol extraction or fingerprinting yet.
          2. Diff against the existing cards: ``uncovered`` files are referenced by NO card;
             ``stale_covered`` files are referenced by a card but their sha256 has changed. Only
             these drive LLM work. If both are empty the store is up to date (return 0).
          3. Run the 3-stage LLM fan-out over just the uncovered files, deduping against the
             existing cards. Separately regenerate the cards that reference stale files.
          4. Fingerprint every referenced file in parallel, build the final JSON, apply the
             ``skip_unchanged`` check, and write atomically. Write ``bootstrap_meta.json`` at the
             end when any card was written.

        With no ``provider`` no cards can be identified, so this returns 0 (a no-op).

        CANCELLATION: ``close()`` stops the pass at the next checkpoint (entry, each walked
        directory, before the LLM fan-out, before the fingerprint pass) and returns 0 rather than
        keep walking and shelling out to git for a store nobody owns any more.
        """
        if self._closed.is_set():
            return 0
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0

        # Resolve the cards_dir so we can skip it if it's inside the walk root.
        cards_dir_resolved = self._cards_dir.resolve()
        skip_dirs = effective_skip_dirs(walk_root)

        # --- Pass 1: walk the tree and collect qualifying source file paths (flat list) ---
        # Which folders are worth indexing AT ALL, decided before a single file is collected, by
        # the ONE gate every card source asks (this walk, nested and ancestor imports, mounts):
        # nested repositories and fuzzy duplicates settled deterministically, folders with a risk
        # signal (many card stores, a series of dated copies) always put to the model and, if it
        # cannot be asked, kept out, and only what is left reviewed as before. See folder_gate.
        if gate is None:
            gate = folder_gate(walk_root, skip_dirs, provider, model, self._cards_dir)
        excluded_folders: Set[str] = gate.excluded

        if max_files is None:
            max_files = _bootstrap_max_files()
        if max_cards is None:
            max_cards = _bootstrap_max_cards()
        file_paths: List[str] = []
        file_count = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(walk_root):
            if (max_files is not None and file_count >= max_files) or self._closed.is_set():
                truncated = truncated or (max_files is not None and file_count >= max_files)
                break
            current_dir = Path(dirpath).resolve()
            # Skip the cards directory itself to avoid indexing stored card JSON files.
            if current_dir == cards_dir_resolved:
                dirnames[:] = []
                continue
            # Prune skip dirs in-place so os.walk doesn't recurse into them.
            prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
            # Drop folders the review excluded, before descending into them.
            if excluded_folders:
                try:
                    here = current_dir.relative_to(walk_root).as_posix()
                except ValueError:
                    here = ""
                # ``relative_to`` renders the root as ".", and "./child" matches a "." prefix, so
                # the root must be the EMPTY string before any path is composed from it.
                if here == ".":
                    here = ""
                if _is_excluded_folder(here, excluded_folders):
                    dirnames[:] = []
                    continue
                dirnames[:] = [
                    d for d in dirnames
                    if not _is_excluded_folder(f"{here}/{d}".lstrip("/"), excluded_folders)
                ]
            # Also exclude the cards dir itself (it's internal state, not source).
            dirnames[:] = [
                d for d in dirnames
                if (current_dir / d).resolve() != cards_dir_resolved
            ]
            for fname in filenames:
                if max_files is not None and file_count >= max_files:
                    truncated = True
                    break
                if is_claude_state_file(fname):
                    continue
                fpath = Path(dirpath) / fname
                if fpath.suffix not in _SOURCE_EXTS:
                    continue
                try:
                    if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                        continue
                except OSError:
                    continue
                file_count += 1
                file_paths.append(str(fpath.relative_to(walk_root)))

        # Announced HERE, immediately after the walk, so no later early-return can skip it: a
        # truncated corpus is the single most misleading state this function can be left in.
        if truncated:
            _log.warning(
                "context index: stopped walking at %d file(s) — QAR_BOOTSTRAP_MAX_FILES limit "
                "reached; files beyond it are NOT indexed and this store is INCOMPLETE",
                file_count,
            )

        if not file_paths or self._closed.is_set():
            return 0

        existing = self._own_cards()
        existing_cards = list(existing.values())
        self._backfill_origins(existing)
        self.limit_guard.begin_pass()

        # --- Nested-store reuse: a sub-corpus already bootstrapped by its OWN QAR instance is
        # reused wholesale instead of re-discovered here. Pure filesystem reuse (no LLM call, no
        # provider needed), so this runs even when this store has none wired. A card previously
        # imported from a nested store that no longer offers it (renamed/deleted/de-bootstrapped)
        # is pruned so imports never drift from their source.
        imported_cards: List[Dict[str, Any]] = []
        imported_covered: Set[str] = set()
        skipped_for_own: List[Dict[str, Any]] = []
        kept_copies: List[str] = []          # copies somebody added to that their source no longer offers
        if self._mount_mode == MODE_MOUNT:
            # Mounted stores are read in place: nothing is copied or pruned here. Files a mounted
            # store already covers (an ancestor's cards over this root) are not this corpus's to card.
            for mounted_view in self._mounted_repo.load_all().values() if self._mounted_repo else []:
                if self._mounted_repo and self._mounted_repo.is_mounted(str(mounted_view.get("id", ""))):
                    imported_covered.update(
                        (fe.get("path") if isinstance(fe, dict) else fe) or ""
                        for fe in mounted_view.get("files", []))
            imported_covered.discard("")
            stale_copies = [c for c in existing_cards
                             if self._card_imported_from(c)]
            if stale_copies:
                _log.warning(
                    "context index: %d namespaced copy card(s) from the old copy-import remain in "
                    "this store alongside the mounts. Run `quest-ai-runner context migrate-imports` "
                    "to convert them; nothing is deleted automatically.", len(stale_copies))
        elif self._reuse_nested_cards:
            for nested_root in self._discover_nested_card_dirs(walk_root, skip_dirs, gate):
                for ic in self._import_nested_cards(nested_root, walk_root):
                    imported_cards.append(ic)
                    imported_covered.update(ic.get("files", []))
            # Mirror image: also look UPWARD for an already-indexed ANCESTOR corpus (this root is
            # the narrower one, e.g. bootstrapping a subfolder of an already-indexed wide corpus).
            # Both directions can contribute at once (an indexed ancestor above AND an indexed
            # descendant below), so this is not an elif.
            ancestor_root = self._discover_ancestor_card_dir(walk_root, gate)
            if ancestor_root is not None:
                for ic in self._import_ancestor_cards(ancestor_root, walk_root, gate):
                    imported_cards.append(ic)
                    imported_covered.update(ic.get("files", []))
            # A card whose every file is gone is dead weight, and is pruned further down. Judge the
            # imports against the cards that will remain, so what is kept does not depend on the order
            # of the steps: a copy of a dead card is not imported (it would only be pruned next pass),
            # and a dead own card does not shadow a live copy of the same files.
            walked_now = set(file_paths)
            dead_now = set(self.dead_card_ids(existing_cards, walked_now, walk_root, skip_dirs)) \
                if _prune_dead_cards() else set()
            imported_cards = [
                ic for ic in imported_cards
                if not _prune_dead_cards()
                or not all(self._file_entry_is_dead(p, walked_now, walk_root, skip_dirs)
                           for p in ic.get("files", []))]
            # One copy per id and per file set, whichever routes offered it (see card_origin).
            imported_cards = card_origin.choose_imports(
                imported_cards, [c for c in existing_cards if c.get("id") not in dead_now], skipped_for_own)
            imported_covered = {path for ic in imported_cards for path in ic.get("files", [])}
            if imported_cards:
                _log.info(
                    "context index: reusing %d card(s) from %d corpus root(s) (nested and/or "
                    "ancestor), covering %d file(s) with zero LLM calls",
                    len(imported_cards),
                    len({ic["imported_from"] for ic in imported_cards}),
                    len(imported_covered),
                )
            imported_ids_now = {ic["id"] for ic in imported_cards}
            retired = [
                c["id"] for c in existing_cards
                if c.get("id") and card_origin.is_copy(c) and c["id"] not in imported_ids_now
                and c["id"] not in dead_now
            ]
            # A copy somebody added to (notes, usage, quest tags) is not retired here: the card that stands
            # for it may not be written yet. After the pass, ``reconcile_copies`` joins it onto that card
            # and then retires it (or leaves it if there is none).
            kept_copies = [cid for cid in retired
                           if card_origin.protection(next((c for c in existing_cards if c.get("id") == cid), {}))]
            retired = [cid for cid in retired if cid not in set(kept_copies)]
            if retired:
                pace = Pacer()
                moved = 0
                for cid in retired:
                    if not self._dry_run:
                        try:
                            moved += 1 if self.retire_copy(cid) else 0
                        except Exception:  # noqa: BLE001
                            pass
                        pace()
                gone = set(retired)
                existing_cards = [c for c in existing_cards if c.get("id") not in gone]
                _log.info("context index: %d copied card(s) are no longer offered by their source "
                          "store; moved out of the index", moved)

        # Topic cards for genuinely NEW content require semantic understanding. Without a
        # provider, only cards reused from a nested store (above) can be produced.
        if provider is None and not imported_cards:
            _log.warning(
                "context index: bootstrap skipped — no model provider wired, so no semantic "
                "topic cards can be identified (cards accumulate via record() instead)"
            )
            return 0

        # Heal cards whose summary is a task prompt rather than a description. Their name and
        # files are good; only the one field is scaffolding, so it is replaced from the card's own
        # grounded fields rather than the card being thrown away.
        if existing_cards:
            healed = 0
            for card in existing_cards:
                summary = card.get("summary") or ""
                if not _is_prompt_echo(summary):
                    continue
                replacement = _first_substantive_line(summary) or (card.get("name") or "").strip()
                if not replacement or replacement == summary.strip():
                    continue
                card["summary"] = replacement[:200]
                cid = card.get("id")
                if cid and not self._dry_run:
                    try:
                        self._store_write(cid, card)
                        healed += 1
                    except Exception:  # noqa: BLE001
                        pass
            if healed:
                _log.info("context index: rewrote %d card summary(ies) that echoed a task prompt "
                          "instead of describing the card", healed)

        # --- Prune cards the corpus no longer indexes -------------------------------------
        #
        # The index was append-only with respect to its own inclusion rules: a card was written
        # when a file was walked, and nothing removed it when that stopped being true. Two ways
        # that happens, both ordinary: the file is DELETED, or the skip list GROWS and the file is
        # now inside an excluded directory (a vendored SDK, a build output). The card then pins a
        # path the corpus will never walk again, and it is not merely useless -- it is carried
        # through every later pass: embedded on every vector seed (the memory bound, see
        # QdrantVectorStore.upsert) and compared against every other card by the O(n^2) keyword
        # clustering in dedup. MEASURED on one real corpus: 3,107 of 5,018 cards (62%) pinned
        # files inside a directory added to the skip list long after they were written, and a
        # further 506 (10%) pinned files that no longer existed -- 72% dead weight, all of it
        # still being paid for on every run.
        # ``--dry-run`` promises to estimate WITHOUT running the bootstrap, so it must not
        # delete either: an estimate that mutates the thing it is estimating is not an estimate.
        if _prune_dead_cards() and existing_cards and not self._dry_run:
            dead_ids = self.dead_card_ids(existing_cards, set(file_paths), walk_root, skip_dirs)
            survivors = [c for c in existing_cards if c.get("id") not in set(dead_ids)]
            if dead_ids:
                removed = 0
                for cid in dead_ids:
                    try:
                        self._repo.delete(cid)
                        removed += 1
                    except Exception:  # noqa: BLE001 -- one undeletable card never stops the rest
                        pass
                _log.info(
                    "context index: pruned %d card(s) that were dead weight — files the corpus no "
                    "longer indexes, or cards that only restated their own path — %d card(s) "
                    "remain", removed, len(survivors),
                )
                existing_cards = survivors
        elif _prune_dead_cards() and existing_cards and self._dry_run:
            walked_now = set(file_paths)
            would = sum(
                1 for card in existing_cards
                if [e for e in ((fe.get("path", "") if isinstance(fe, dict) else fe)
                                for fe in card.get("files", [])) if e]
                and all(self._file_entry_is_dead(e, walked_now, walk_root, skip_dirs)
                        for e in [x for x in ((fe.get("path", "") if isinstance(fe, dict) else fe)
                                              for fe in card.get("files", [])) if x])
            )
            if would:
                _log.info("context index: DRY RUN — %d card(s) would be pruned (files deleted, or "
                          "inside a directory now skipped); nothing was deleted", would)

        # --- Incremental diff: what is uncovered (in no card) vs stale (covered but changed) ---
        covered: Set[str] = set()
        for card in existing_cards:
            for fe in card.get("files", []):
                p = fe.get("path", "") if isinstance(fe, dict) else fe
                if p:
                    covered.add(p)
        # A copy of another store's card is that store's to keep fresh: this store re-imports it
        # when it changes. Nothing below regenerates, migrates or merges new topics into one.
        own_cards = [c for c in existing_cards if not card_origin.is_copy(c)]

        uncovered = [p for p in file_paths if p not in covered and p not in imported_covered]
        # A covered file is stale when its current sha256 differs from any card's stored sha.
        stale_covered: List[str] = []
        if covered:
            walked = set(file_paths)
            for card in own_cards:
                for fe in card.get("files", []):
                    if not isinstance(fe, dict):
                        continue
                    p = fe.get("path", "")
                    if not p or p not in walked:
                        continue
                    stored = fe.get("sha256", "")
                    if not stored:
                        continue
                    cur = self._current_sha(p, fe)
                    if cur and cur != stored and p not in stale_covered:
                        stale_covered.append(p)

        # On the very first bootstrap (no existing cards) everything not covered by an import
        # is "uncovered".
        if not existing_cards:
            uncovered = [p for p in file_paths if p not in imported_covered]

        discovered = self._read_discovered()
        if discovered and uncovered:
            before = len(uncovered)
            uncovered = [p for p in uncovered if not self._discovered_unchanged(p, discovered)]
            if before != len(uncovered):
                _log.info("context index: %d file(s) already shown to topic discovery and "
                          "unchanged since; not re-analysed", before - len(uncovered))

        # --- Feature migration: cards whose file entries have an outdated per-feature version ---
        # These cards already have correct LLM-generated content; only the cheap computed fields
        # (e.g. tfdfidf_terms) need refreshing.  They are added to topic_cards WITHOUT triggering
        # LLM calls — stage 4b recomputes the field, stage 5 writes the updated card.
        # The per-entry "tfdfidf_v" field IS the checkpoint: entries already at _TFDFIDF_VERSION
        # are skipped automatically, so an interrupted migration resumes on the next startup.
        tfdfidf_migration_cards: List[Dict[str, Any]] = []
        for card in own_cards:
            cid = card.get("id", "")
            if not cid:
                continue
            card_files = [fe for fe in card.get("files", []) if isinstance(fe, dict) and fe.get("path")]
            if any(fe.get("tfdfidf_v", 0) < _TFDFIDF_VERSION for fe in card_files):
                tfdfidf_migration_cards.append({
                    "id": cid,
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description", ""),
                    "files": [fe["path"] for fe in card_files],
                })
        if tfdfidf_migration_cards:
            _log.info(
                "context index: %d card(s) have file entries below tfdfidf v%d — will update",
                len(tfdfidf_migration_cards), _TFDFIDF_VERSION,
            )

        if not uncovered and not stale_covered and not tfdfidf_migration_cards and not imported_cards:
            _log.info("context index: all files covered and up to date")
            if kept_copies and not self._dry_run:
                self.reconcile_copies()
            return 0

        _log.info(
            "context index: %d new file(s) found (not in any existing card), %d stale file(s) "
            "— processing", len(uncovered), len(stale_covered),
        )

        # --- LLM: identify topic cards for the NEW (uncovered) files, deduping vs existing ---
        topic_cards: List[Dict[str, Any]] = []
        room = self.limit_guard.room()
        if uncovered and room is not None and room <= 0:
            # The store is full: a new topic card would be refused, so do not pay a model to write it.
            # Said once, with the breakdown (see card_limit); the files stay unindexed, not "seen".
            self.limit_guard.note_unindexed(len(uncovered))
            uncovered_discoverable = False
        else:
            uncovered_discoverable = True
        analysed = list(uncovered)
        if uncovered and uncovered_discoverable and room is not None and provider is not None:
            # Room for only so many cards: the model is asked about that many folders (the most recently
            # changed first), not about every folder and left to have most of its cards refused.
            analysed, waiting_files = folders_for_room(uncovered, room, walk_root)
            if waiting_files:
                self.limit_guard.note_unindexed(len(waiting_files))
                _log.info("context index: room for %d more card(s); analysing the %d file(s) of the most "
                          "recently changed folders, %d file(s) wait for room", room, len(analysed), len(waiting_files))
        if uncovered and provider is not None and uncovered_discoverable:
            _log.info("context index: stage 2 — analyzing %d new files for topics", len(analysed))
            topic_cards = _llm_topic_cards(
                analysed, provider, model=model, existing_cards=own_cards, walk_root=walk_root
            )
            _log.info("context index: identified %d topic card(s) from new files", len(topic_cards))
        discovered_now: List[str] = list(analysed) if (uncovered and topic_cards) else []

        # Merge nested-imported cards now (before stale-covered regen below): a file re-imported
        # unchanged from a nested store can also show up in ``stale_covered`` versus THIS store's
        # own last-written fingerprint of it, and the regen loop already skips any id present in
        # ``topic_cards`` — this avoids a wasted LLM regen call on content that isn't actually new,
        # just imported. Different corpus roots never legitimately share a topic, so no LLM dedup
        # is needed here.
        if imported_cards:
            queued_ids = {tc.get("id") for tc in topic_cards}
            stored_by_id = {c.get("id"): c for c in existing_cards if c.get("id")}
            for ic in imported_cards:
                if ic.get("id") in queued_ids:
                    continue
                if self._imported_card_unchanged(ic, stored_by_id.get(ic.get("id"))):
                    continue
                topic_cards.append(ic)

        # --- Stale-covered: regenerate the cards that reference any stale file ---
        # Identify the cards touching a stale file and re-run topic extraction over each card's
        # file set so its summary/keywords/files reflect the current code, keeping the card id.
        if stale_covered and provider is not None:
            _log.info("context index: stage 3 — regenerating %d stale card(s)", len(stale_covered))
            stale_set = set(stale_covered)
            regen_ids = {
                card.get("id")
                for card in own_cards
                for fe in card.get("files", [])
                if isinstance(fe, dict) and fe.get("path", "") in stale_set
            }
            existing_ids_new = {tc.get("id") for tc in topic_cards}
            for card in own_cards:
                cid = card.get("id")
                if cid not in regen_ids or cid in existing_ids_new:
                    continue
                files = [
                    fe.get("path", "") for fe in card.get("files", [])
                    if isinstance(fe, dict) and fe.get("path", "")
                ]
                files = [f for f in files if f]
                if not files:
                    continue
                area = {
                    "name": card.get("name", cid),
                    "description": card.get("summary", ""),
                    "files": files,
                }
                regenerated = _extract_topic_cards(area, set(files), provider, model, walk_root=walk_root)
                if regenerated:
                    # Replace the card IN PLACE with exactly one card under its own id. Any extra
                    # topics the model returns used to be written as brand-new cards, so a file that
                    # changes on every run (a state JSON) grew its card count each bootstrap, and
                    # those new cards went stale and spawned more: one tracker file ended up
                    # described by 1,296 cards. New topics belong to NEW files (stage 2 above).
                    regenerated[0]["id"] = cid
                    topic_cards.append(regenerated[0])
                else:
                    # Extraction failed: keep the card but re-pin (rebuild from its own fields).
                    topic_cards.append({
                        "id": cid,
                        "name": card.get("name", ""),
                        "keywords": card.get("keywords", []),
                        "summary": card.get("summary", ""),
                        "files": files,
                    })

        # Merge tfdfidf migration cards (don't add duplicates already queued for LLM regen).
        if tfdfidf_migration_cards:
            queued_ids = {tc.get("id") for tc in topic_cards}
            for mc in tfdfidf_migration_cards:
                if mc.get("id") not in queued_ids:
                    topic_cards.append(mc)

        # --- The three inflation gates, between "what the model proposed" and "what is written" --
        # Deliberately here rather than inside the LLM stages: a card can also arrive from the
        # stale-regen path, the import path or the tfdfidf migration, and every one of them has
        # grown a store past its budget at some point.
        topic_cards = self._gate_new_cards(topic_cards, existing_cards,
                                           max_cards=max_cards)

        if not topic_cards:
            self.limit_guard.end_pass()
            if kept_copies and not self._dry_run:
                self.reconcile_copies()
            return 0

        self._cards_dir.mkdir(parents=True, exist_ok=True)

        # --- Pass 2: fingerprint every file referenced by any topic card, in parallel ---
        # Checkpoint: the fingerprint pass is the git-subprocess-heavy one, so a closed store stops
        # before it rather than fanning out ``git hash-object`` calls it will never use.
        if self._closed.is_set():
            return 0
        referenced: List[str] = []
        seen: Set[str] = set()
        for tc in topic_cards:
            # Normalised rather than trusted: a card reaching here can have come from the model
            # (plain paths) or from disk via a dedup merge (dicts). See _card_file_paths.
            for rel in _card_file_paths(tc):
                if rel not in seen:
                    seen.add(rel)
                    referenced.append(rel)

        fp_map: Dict[str, Dict[str, Any]] = {rel: {} for rel in referenced}
        if referenced:
            _log.info("context index: stage 4 — fingerprinting %d file(s)", len(referenced))
            n_workers = min(8, len(referenced))
            fp_results = _run_parallel(
                [lambda r=rel: (r, self._fingerprint(r)) for rel in referenced],
                max_workers=n_workers,
            )
            for item in fp_results:
                if item is not None:
                    rel, fp = item
                    fp_map[rel] = fp or {}

        # --- Pass 2b: compute per-file TF-DF-IDF term signatures from actual content ---
        # Read each referenced file's content, compute corpus-level DF, then compute the
        # top-15 most distinctive terms per file (high TF in this file, rare across corpus).
        # These are stored as ``tfdfidf_terms`` in each file entry so ``_card_term_weights``
        # can use actual content signal instead of relying only on LLM-assigned keywords.
        tfdfidf_terms_map: Dict[str, List[str]] = {}
        if referenced and walk_root is not None:
            _log.info("context index: stage 4b — computing TF-DF-IDF term signatures for %d file(s)", len(referenced))

            def _read_file_terms(rel: str) -> tuple:
                try:
                    p = walk_root / rel
                    if p.exists() and p.stat().st_size <= _BOOTSTRAP_MAX_BYTES:
                        text = p.read_text(encoding="utf-8", errors="replace")
                        return rel, _tokenize(text)
                except Exception:  # noqa: BLE001
                    pass
                return rel, set()

            n_workers_content = min(8, len(referenced))
            content_results = _run_parallel(
                [lambda r=rel: _read_file_terms(r) for rel in referenced],
                max_workers=n_workers_content,
            )

            # Build corpus DF (how many files contain each term)
            file_term_sets: Dict[str, Set[str]] = {}
            for item in content_results:
                if item is not None:
                    rel, terms = item
                    file_term_sets[rel] = terms
            corpus_df_content: Dict[str, int] = {}
            for terms in file_term_sets.values():
                for t in terms:
                    corpus_df_content[t] = corpus_df_content.get(t, 0) + 1
            N_corpus = max(len(file_term_sets), 1)

            # Compute top-15 distinctive terms per file: IDF = log((N+1)/(df+1))+1, TF=presence
            for rel, terms in file_term_sets.items():
                if not terms:
                    tfdfidf_terms_map[rel] = []
                    continue
                term_scores = {
                    t: math.log((N_corpus + 1) / (corpus_df_content.get(t, 0) + 1)) + 1
                    for t in terms
                }
                top = sorted(term_scores, key=term_scores.__getitem__, reverse=True)[:15]
                tfdfidf_terms_map[rel] = top

        # --- Pass 3: build and write one card per topic ---
        _log.info("context index: stage 5 — writing %d card(s)", len(topic_cards))
        cards_written = 0
        for tc in topic_cards:
            if max_cards is not None and cards_written >= max_cards:
                _log.warning(
                    "context index: stopped after writing %d card(s) — QAR_BOOTSTRAP_MAX_CARDS "
                    "limit reached with %d still to write; the store is INCOMPLETE",
                    cards_written, len(topic_cards) - cards_written,
                )
                break

            card_id = tc["id"]
            rels = tc.get("files", [])

            file_dicts: List[Dict[str, Any]] = []
            for rel in rels:
                fp = fp_map.get(rel, {})
                file_dicts.append({
                    "path": rel,
                    "sha256": fp.get("sha256", ""),
                    "mtime": fp.get("mtime", 0.0),
                    "git_sha": fp.get("git_sha", ""),
                    "why": "",
                    "symbols": [],
                    "tfdfidf_terms": tfdfidf_terms_map.get(rel, []),
                    "tfdfidf_v": _TFDFIDF_VERSION,
                })

            # Load existing card so we preserve usage_count / last_outcome if present.
            loaded_existing = self._repo.read(card_id)
            if (self._injected_repo and isinstance(loaded_existing, dict)
                    and not self._is_own_card(loaded_existing)):
                # The id is taken by another origin's card in the shared repository. Never replace it:
                # this lane's card gets its own, deterministic id instead.
                card_id = f"{card_id}-{hashlib.sha1(self.origin_id().encode()).hexdigest()[:6]}"
                loaded_existing = self._repo.read(card_id)
            if (not tc.get("origin_fields") and isinstance(loaded_existing, dict)
                    and card_origin.is_copy(loaded_existing)):
                # The id belongs to a copy of another store's card. A card that is not that copy never
                # takes it over (it would keep the copy's name and lose its origin, and the copy is
                # then nobody's): it gets an id of its own, from the files it pins.
                digest = hashlib.sha1("\0".join(sorted(rels)).encode("utf-8", "replace")).hexdigest()[:6]
                card_id = f"{card_id}-{digest}"
                loaded_existing = self._repo.read(card_id)
            existing: Dict[str, Any] = loaded_existing if isinstance(loaded_existing, dict) else {}

            # Incremental refresh: skip a topic only if every file's sha256 is unchanged AND
            # every file entry already carries the current per-feature versions.
            # An import whose stored copy does not say where it came from (written by an older version) is
            # never "unchanged": it is written once so that the copy names its origin.
            stored_prov = card_origin.provenance_of(existing)
            names_its_origin = all(stored_prov.get(k) == v for k, v in (tc.get("origin_fields") or {}).items())
            if skip_unchanged and existing and names_its_origin:
                old_file_data = {
                    fe.get("path", ""): fe
                    for fe in existing.get("files", [])
                    if isinstance(fe, dict)
                }
                unchanged = bool(file_dicts) and all(
                    fd["sha256"]
                    and old_file_data.get(fd["path"], {}).get("sha256", "") == fd["sha256"]
                    and old_file_data.get(fd["path"], {}).get("tfdfidf_v", 0) >= _TFDFIDF_VERSION
                    for fd in file_dicts
                ) and len(old_file_data) == len(file_dicts)
                if unchanged:
                    continue

            # A card that already exists keeps what it was given by someone else: the notes a run added,
            # the quest fence, who owns it, and who wrote it. Rewriting it from the topic alone turned a
            # card learned from a run (one that pins files) into a "bootstrap" card at the next pass and
            # dropped its notes, after which the card limit would have treated it as regenerable.
            prior = card_origin.provenance_of(existing)
            kept = {k: existing[k] for k in ("content", "scope_tags", "managed_by", "managed_fields",
                                             "managed_items", "origin") if k in existing}
            card: Dict[str, Any] = {
                **kept,
                "id": card_id,
                "name": tc.get("name", ""),
                "keywords": tc.get("keywords", []),
                "summary": tc.get("summary", ""),
                "description": tc.get("summary", ""),
                "files": file_dicts,
                "conventions": [],
                "provenance": {
                    **prior,
                    # A card that already exists keeps who wrote it; one with no author at all (written by a
                    # person) is not turned into an index-pass card by being refreshed, which would make it
                    # movable. Only a new card is "bootstrap".
                    "created_by_task": prior.get("created_by_task") or (
                        "" if existing and not tc.get("origin_fields") else "bootstrap"),
                    "model": prior.get("model", ""),
                    "created_at": prior.get("created_at", ""),
                    "last_verified_at": prior.get("last_verified_at", ""),
                    **({"imported_from": tc["imported_from"]} if tc.get("imported_from") else {}),
                    **(tc.get("origin_fields") or {}),
                },
                "usage_count": existing.get("usage_count", 0),
                "last_outcome": existing.get("last_outcome", "unknown"),
            }

            card_origin.stamp_created_at(card, loaded_existing)

            # Persist via the repository (skip if dry-run mode).
            if not self._dry_run:
                if self._store_write(card_id, card):
                    cards_written += 1
            else:
                # In dry-run mode, count the card but don't write it.
                cards_written += 1

        # A pass that left cards unwritten for lack of room records nothing as seen: those files are
        # not "analysed and unchanged", they are waiting, and raising the limit must pick them up.
        refused = self.limit_guard.end_pass()
        # Recorded last, once the cards are written, so an interrupted pass re-discovers.
        if discovered_now and not self._dry_run and not refused:
            self._record_discovered(discovered_now, discovered, set(file_paths))

        # Invalidate cache after all writes.
        self._cache_dirty = True
        if (cards_written or kept_copies) and not self._dry_run:
            self.reconcile_copies()
            if cards_written and skipped_for_own and not self.in_second_pass:
                # An import was left out because a card of this store's own pinned the same files, and
                # this pass has just changed the own cards. Settle it now, so a second run of the same
                # pass finds nothing to do: one more look, which is cheap (nothing is uncovered).
                own_now = {card_origin.file_set(c) for c in self._own_cards().values()
                           if not card_origin.is_copy(c)}
                if any(card_origin.file_set(item) not in own_now for item in skipped_for_own):
                    self.in_second_pass = True
                    try:
                        cards_written += self._bootstrap_walk(
                            root, provider=provider, model=model, max_files=max_files,
                            max_cards=max_cards, skip_unchanged=True)
                    finally:
                        self.in_second_pass = False
        return cards_written
