"""FilesWriter — the reference ``FileWriter``: bounded, backed-up writes into a configured root.

This is quest-ai-runner's FIRST write into a consumer's own files. Everything else in the library
that opens a file for writing writes the runner's OWN state (context cards, conversation archives,
bootstrap metadata); this is the one component that changes the corpus, and it is deliberately
small enough to read in one sitting.

Three properties make it safe to hand a model:

1. CONTAINMENT — every path goes through ``files_adapter.resolve_in_tree``, the SAME function the
   read adapter uses. Resolution follows symlinks and normalizes ``..`` before the containment
   test, so a link out of the tree, a traversal, and an absolute path outside the root are all
   refused identically. There is one implementation of this boundary in the repo, not two.
2. SECRET REFUSAL — the same ``_is_secretish`` check the read side applies. A write may not touch
   ``.env*``, ``*.key``, ``*.pem``, or anything named like a secret/credential/password, even
   inside the root.
3. RECOVERABILITY — the previous content is copied to a backup before it is replaced, OUTSIDE the
   corpus. See ``backup_dir`` below for why this is not left to git.

It is also opt-in end to end: constructing a ``FilesWriter`` is the act of granting write access.
A consumer that never constructs one has no path through this library that can modify a file.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..core.adapters import FileWriterBase, WriteResult
from .files_adapter import _BINARY_EXTS, resolve_in_tree

_log = logging.getLogger("quest-ai-runner.files_writer")

# Refuse to write more than this into one file. A fast edit rewrites a source/doc file; a payload
# this large is a sign something went wrong (a runaway generation, a binary pasted as text), and
# the failure should be a refusal rather than a very large mistake.
DEFAULT_MAX_WRITE_BYTES = 2 * 1024 * 1024

# Where previous content is kept. Deliberately OUTSIDE the corpus root, under the same
# ``~/.quest-ai-runner`` the rest of the library already uses for its own state, for two reasons:
# a backup inside the tree would be picked up by the corpus indexers and read back as if it were
# content, and it would show up in the consumer's own version control as an untracked file.
DEFAULT_BACKUP_DIR_ENV_VAR = "QAR_FILE_BACKUP_DIR"


def default_backup_dir() -> Path:
    configured = os.getenv(DEFAULT_BACKUP_DIR_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".quest-ai-runner" / "file-backups"


class FilesWriter(FileWriterBase):
    """Write text files inside ``root``, keeping a backup of what was replaced.

    Args:
        root: the write boundary. Nothing outside it can be written, whatever a path claims.
        backup_dir: where replaced content is copied. ``None`` disables backups entirely, which a
            consumer should only choose when it has its own recovery story (a clean git tree it
            controls, a snapshotting filesystem). It is NOT the default: this library cannot tell
            whether a given corpus root is under version control at all — a synced quest folder, a
            Drive mirror, or a plain docs directory frequently is not — so "git will save you" is
            an assumption it is not entitled to make on the consumer's behalf.
        max_write_bytes: per-write size ceiling.
        allow_create: whether writing a path that does not exist yet is permitted. True by
            default (creating a file is an ordinary edit); a consumer that wants edits confined to
            files that already exist sets this False.
    """

    def __init__(self, root: str, *,
                 backup_dir: Optional[str] = None,
                 backups_enabled: bool = True,
                 max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
                 allow_create: bool = True):
        self.root = Path(root).resolve()
        self.backups_enabled = backups_enabled
        self.backup_dir = (Path(backup_dir).expanduser() if backup_dir
                           else default_backup_dir()) if backups_enabled else None
        self.max_write_bytes = max_write_bytes
        self.allow_create = allow_create

    # --- helpers -------------------------------------------------------------

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return path.name

    def resolve(self, rel_path: str) -> Optional[Path]:
        """The boundary, exposed so a caller can pre-screen a candidate path without writing."""
        return resolve_in_tree(self.root, rel_path)

    def exists(self, rel_path: str) -> bool:
        resolved = self.resolve(rel_path)
        return bool(resolved and resolved.is_file())

    # --- FileWriter API ------------------------------------------------------

    def read_file(self, rel_path: str) -> Optional[str]:
        resolved = self.resolve(rel_path)
        if resolved is None or not resolved.is_file():
            return None
        if resolved.suffix.lower() in _BINARY_EXTS:
            return None
        try:
            return resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def write_file(self, rel_path: str, content: str) -> WriteResult:
        resolved = self.resolve(rel_path)
        if resolved is None:
            # One message for all three refusals (outside root, traversal, secret-ish name) on
            # purpose: a caller that is probing the boundary learns nothing from a finer-grained
            # answer, and the log below carries the detail for the operator.
            _log.warning("refused write to %r: outside root %s, or a protected filename",
                         rel_path, self.root)
            return WriteResult(ok=False, rel_path=rel_path,
                               error="path is outside the writable root, or is a protected file")
        if resolved.is_dir():
            return WriteResult(ok=False, rel_path=rel_path, error="path is a directory")
        if resolved.suffix.lower() in _BINARY_EXTS:
            return WriteResult(ok=False, rel_path=rel_path, error="refusing to write a binary file")
        if content is None:
            return WriteResult(ok=False, rel_path=rel_path, error="no content to write")

        data = content.encode("utf-8")
        if len(data) > self.max_write_bytes:
            return WriteResult(ok=False, rel_path=rel_path,
                               error=f"content is {len(data)} bytes, over the "
                                     f"{self.max_write_bytes}-byte write limit")

        existed = resolved.is_file()
        if not existed and not self.allow_create:
            return WriteResult(ok=False, rel_path=rel_path,
                               error="file does not exist and creating files is disabled")

        backup_path: Optional[str] = None
        if existed:
            backup_path = self.backup(resolved)
            if self.backups_enabled and backup_path is None:
                # A backup that was ASKED FOR and could not be made is a refusal, not a warning.
                # Writing anyway would quietly convert "recoverable edit" into "destructive edit",
                # which is exactly the guarantee the consumer opted into.
                return WriteResult(ok=False, rel_path=rel_path,
                                   error="could not write a backup of the existing file; "
                                         "refusing to overwrite it")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as e:
            return WriteResult(ok=False, rel_path=rel_path,
                               error=f"write failed: {type(e).__name__}", backup_path=backup_path)
        return WriteResult(ok=True, rel_path=self.rel(resolved), bytes_written=len(data),
                           created=not existed, backup_path=backup_path)

    # --- backups -------------------------------------------------------------

    def backup(self, resolved: Path) -> Optional[str]:
        """Copy ``resolved``'s current content into the backup dir. Returns the backup path.

        Flat directory, one file per write, named ``<epoch-ms>__<rel-path-with-separators-flattened>``
        so the newest copy of any file sorts last and the original location is readable from the
        name alone. Returns None when backups are disabled (nothing was asked for) or when the copy
        failed (the caller treats that as a refusal, above).
        """
        if not self.backups_enabled or self.backup_dir is None:
            return None
        try:
            rel = self.rel(resolved).replace(os.sep, "__").replace("/", "__")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            target = self.backup_dir / f"{int(time.time() * 1000)}__{rel}"
            target.write_bytes(resolved.read_bytes())
            return str(target)
        except OSError:
            _log.warning("could not back up %s before overwriting it", resolved, exc_info=True)
            return None
