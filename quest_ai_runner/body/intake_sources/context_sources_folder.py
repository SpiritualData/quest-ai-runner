"""``folder_files``: files added or changed in the quest's working folder under ``human_context/``.

A meeting router (or the person) drops material the quest's work should know about into
``<folder>/human_context/``: transcript excerpts, forwarded notes, a pasted thread. None of it passes
through Quest, so no other source can see it. This one lists what appeared or changed since the
watermark: path, modified time, size and the first ~600 characters, so a run can open the file when the
excerpt says it matters.

``human_context/from_quest/`` is excluded: ``sync-quest-folder`` writes it FROM Quest, so reading it back
would present the quest's own notes to itself as news.

The folder comes from the engine (the deployment's ``quest_folder_map``, or a caller's ``--folder``);
a spec may also name one (``{"source": "folder_files", "folder": "..."}``). No folder, no files: an empty
result, never an error.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

from quest_ai_runner.body.intake_sources.context_sources_base import _BaseSource
from quest_ai_runner.body.intake_sources.context_update_state import CollectRequest
from quest_ai_runner.body.intake_sources.context_update_types import ContextUpdate

EXCERPT_CHARS = 600
MAX_FILES = 12
SUBFOLDER = "human_context"
EXCLUDED = ("from_quest",)
SKIP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".mp3", ".mp4", ".wav", ".docx", ".xlsx", ".pyc"})


def changed_files(folder: str, since: datetime) -> List[Tuple[Path, float, int]]:
    """``[(path, mtime, size)]`` under ``<folder>/human_context`` modified after ``since``, newest first."""
    root = Path(folder).expanduser() / SUBFOLDER
    if not root.is_dir():
        return []
    found: List[Tuple[Path, float, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED and not d.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            if name.startswith(".") or path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > since.timestamp():
                found.append((path, stat.st_mtime, stat.st_size))
    return sorted(found, key=lambda f: f[1], reverse=True)


class FolderFilesSource(_BaseSource):
    """Files that appeared or changed in the quest folder's ``human_context/`` since the watermark."""
    name = "folder_files"
    describes = "files added or changed in the quest working folder's human_context/ (not from_quest/)"
    judge_relevance = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        folder = str(request.opt("folder") or request.folder or "").strip()
        if not folder or request.since is None:
            return []
        files = changed_files(folder, request.since)
        root = Path(folder).expanduser()
        out: List[ContextUpdate] = []
        for path, mtime, size in files[:MAX_FILES]:
            try:
                head = path.read_text(errors="replace")[:EXCERPT_CHARS]
            except OSError:
                continue
            rel = str(path.relative_to(root))
            when = datetime.fromtimestamp(mtime, tz=timezone.utc)
            body = f"{rel}  (modified {when.strftime('%Y-%m-%d %H:%M')} UTC, {size} bytes)\n\n{head.strip()}"
            if size > len(head.encode("utf-8", "ignore")):
                body += "\n[excerpt only: open the file for the rest]"
            out.append(ContextUpdate(
                source=self.name, kind="file", item_id=rel, title=rel, body=body, verbatim=True,
                excerpt=head.strip().splitlines()[0] if head.strip() else rel, occurred_at=when,
                location=rel, summary=f"{rel}", raw={"path": str(path), "size": size}))
        request.account(len(files), f"{len(files)} file(s) changed in human_context/, {len(out)} shown")
        return out
