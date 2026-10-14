"""The bootstrap lifecycle: stale cards, prune, refresh, nested and ancestor card imports.

A mixin of ``FileContextStore`` (see ``file_context_store.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

from .card_repository import FilesystemCardRepository
from .card_store_mounts import MODE_MOUNT, resolve_mounts
from .card_content_render import MAX_CARD_REF_ITEM_CHARS as _MAX_CARD_REF_ITEM_CHARS
from ._walk import effective_skip_dirs, is_claude_state_file, prune_dirnames, skip_subtrees
from .card_store_identity import is_merged as is_merged_store, read_meta as read_store_meta
from .bootstrap_reporter import mark_bootstrap_completed
from ..core.file_modes import match_umask
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
from .context_card_extraction import _path_slug


class BootstrapMixin:
    """The bootstrap lifecycle: stale cards, prune, refresh, nested and ancestor card imports."""

    # ------------------------------------------------------------------
    # Public helper: O(1) invalidation index
    # ------------------------------------------------------------------

    def stale_cards_for(self, path: str) -> Set[str]:
        """Return card ids that pin ``path`` AND whose fingerprint for it is now stale.

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
                        if cur and fe.get("sha256") != cur:
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
                    card["keywords"] = kws[:_MAX_MERGED_KEYWORDS]
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
                        card["summary"] = replacement[:200]
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
            return self._bootstrap_inner(root=root, provider=provider, model=model, skip_unchanged=True)
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

    def _discover_nested_card_dirs(self, walk_root: Path, skip_dirs: Set[str]) -> List[Path]:
        """Find sub-corpora under ``walk_root`` that already have their OWN completed bootstrap.

        A directory qualifies when ``<dir>/.quest-context/bootstrap_meta.json`` exists — the
        signal a DIFFERENT (usually narrower-scoped) ``FileContextStore`` already indexed it, e.g.
        a ``product/`` corpus nested under a wider ``~/hq`` corpus. Recursion stops at each match:
        a nested store is trusted to have already reused whatever is bootstrapped beneath IT (if it
        implements the same reuse), so importing its cards transitively covers its own subtree too.
        Never raises; returns [] on any failure.
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
                if (current_dir / ".quest-context" / _BOOTSTRAP_META_FILE).is_file() \
                        and not is_merged_store(str(current_dir / ".quest-context")):
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

    def _import_nested_cards(self, nested_root: Path, walk_root: Path) -> List[Dict[str, Any]]:
        """Reuse a nested corpus's already-bootstrapped cards instead of re-discovering them.

        Pure filesystem reuse: reads the nested store's on-disk cards directly (no LLM call), then
        rewrites each file path from nested-root-relative to THIS store's walk-root-relative and
        namespaces the card id by the nested root's path so ids from unrelated sub-corpora never
        collide. Each returned dict carries ``imported_from`` (the nested root, relative to
        ``walk_root``) so ``_bootstrap_inner`` can both write it into the card's provenance and
        prune it later if the nested store stops offering it. Never raises; returns [] on failure.
        """
        try:
            rel_root = nested_root.relative_to(walk_root)
        except ValueError:
            return []
        prefix = rel_root.as_posix()
        id_prefix = _path_slug(prefix)
        try:
            nested_cards = FilesystemCardRepository(str(nested_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
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
                # Never import a card that carries nothing but its own path. Reuse is a
                # shortcut past LLM work, not a licence to spread another store's dead weight:
                # an ancestor store full of these re-seeded the corpus below it on every
                # bootstrap, silently undoing each prune. Judged on the SOURCE card, before its
                # paths are rewritten, so the summary and the path are still in the same terms.
                # Never re-import a card that came FROM the opposite direction (copied down from an ancestor). Two
                # stores that are each other's ancestor/nested source (spiritual_data over product/)
                # otherwise re-import each other's imports every bootstrap, each round wrapping the
                # id in another namespace prefix: ~8,700 real cards became 31,600 copies up to eight
                # prefixes deep. File paths round-trip unchanged, so the copies were the same cards
                # under new ids, each embedded and scored separately. Same-direction chains stay
                # (a nested store reusing ITS nested store is the intended transitive reuse).
                if self._card_imported_from(card).startswith(".."):
                    continue
                if self._card_is_degenerate(card):
                    continue
                cid = str(card.get("id") or "")
                if not cid:
                    continue
                imported.append({
                    "id": f"{id_prefix}--{cid}"[:200],
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description") or card.get("summary", ""),
                    "files": files,
                    "imported_from": prefix,
                })
            except Exception:  # noqa: BLE001
                continue
        return imported

    def _discover_ancestor_card_dir(self, walk_root: Path) -> Optional[Path]:
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
        reused whatever is beneath IT. Bounded by ``_MAX_ANCESTOR_WALK_LEVELS`` and stops at the
        filesystem root either way. Never raises; returns ``None`` on any failure or no match.
        """
        try:
            current = walk_root.resolve().parent
            for _ in range(_MAX_ANCESTOR_WALK_LEVELS):
                if (current / ".quest-context" / _BOOTSTRAP_META_FILE).is_file() \
                        and not is_merged_store(str(current / ".quest-context")):
                    return current
                parent = current.parent
                if parent == current:  # reached filesystem root
                    break
                current = parent
        except Exception:  # noqa: BLE001
            pass
        return None

    def _import_ancestor_cards(self, ancestor_root: Path, walk_root: Path) -> List[Dict[str, Any]]:
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
        id_prefix = _path_slug(imported_from)
        try:
            ancestor_cards = FilesystemCardRepository(str(ancestor_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
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
                    files.append(rel.as_posix())
                if not files:
                    continue
                # Never import a card that carries nothing but its own path. Reuse is a
                # shortcut past LLM work, not a licence to spread another store's dead weight:
                # an ancestor store full of these re-seeded the corpus below it on every
                # bootstrap, silently undoing each prune. Judged on the SOURCE card, before its
                # paths are rewritten, so the summary and the path are still in the same terms.
                # Never re-import a card that came FROM the opposite direction (copied up from a descendant). Two
                # stores that are each other's ancestor/nested source (spiritual_data over product/)
                # otherwise re-import each other's imports every bootstrap, each round wrapping the
                # id in another namespace prefix: ~8,700 real cards became 31,600 copies up to eight
                # prefixes deep. File paths round-trip unchanged, so the copies were the same cards
                # under new ids, each embedded and scored separately. Same-direction chains stay
                # (a nested store reusing ITS nested store is the intended transitive reuse).
                if self._card_imported_from(card) and not self._card_imported_from(card).startswith(".."):
                    continue
                if self._card_is_degenerate(card):
                    continue
                cid = str(card.get("id") or "")
                if not cid:
                    continue
                imported.append({
                    "id": f"{id_prefix}--{cid}"[:200],
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description") or card.get("summary", ""),
                    "files": files,
                    "imported_from": imported_from,
                })
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
        if self._mount_mode != MODE_MOUNT:
            return self._bootstrap_walk(root, provider=provider, model=model, max_files=max_files,
                                        max_cards=max_cards, skip_unchanged=skip_unchanged)
        if self._closed.is_set():
            return 0
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0
        outcome = resolve_mounts(str(self._cards_dir), walk_root, effective_skip_dirs(walk_root))
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
                                        max_cards=max_cards, skip_unchanged=skip_unchanged)

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

    def prewarm(self) -> None:
        """Load the cards and build the scoring index ahead of the first turn. Never raises.

        A cold first turn otherwise pays for parsing every card and weighting every term (~7s on
        a 5,800-card store) inside its turn-start context budget. Meant to run in a background
        thread while the session waits for its first message.
        """
        try:
            cards = self._load_all()
            if cards:
                self._scoring_index(cards)
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
