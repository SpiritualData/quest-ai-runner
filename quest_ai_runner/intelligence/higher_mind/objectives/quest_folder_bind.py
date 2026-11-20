"""quest_folder_bind — give a quest with no folder its own folder, safely, in one call.

A lane's ``quest_folder_map_file`` is a JSON ``{quest_id: folder}``. A quest that is not in it has
no home, so its autopilot run has nowhere to keep its work. Two operations close that gap:

    folder_candidates(root, map_file)                       # READ ONLY: folders that look like a quest home
    bind_quest_folder(map_file, quest_id, folder, root=..)  # the ONE safe write: record + scaffold + sync

``bind_quest_folder`` is the documented safe operation for changing the map file. It merges ONE
entry under a file lock, through a temp file and a rename, so it never drops another entry and a
concurrent bind by another run cannot lose an update. It refuses a folder that is already bound to a
different quest, refuses to move a quest that is already bound elsewhere, and refuses any folder that
does not resolve inside ``root`` (a lane never writes outside its own zone). Binding the same pair
twice is a no-op that still refreshes the folder's sync.

Never edit the map file by hand or rebuild this logic in a script: call this.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from quest_ai_runner.intelligence.higher_mind.objectives.quest_folder_sync import (
    QuestFolderSyncResult, quest_id_in_folder, sync_quest_folder)
from quest_ai_runner.intelligence.higher_mind.objectives.quest_folder_zones import ensure_folder_zones

log = logging.getLogger("quest-ai-runner.quest_folder_bind")

#: Files that mark a folder as already being a home for some piece of work.
HOME_MARKERS = ("CLAUDE.md", "QUEST_SYNC.md", "GOALS.md")
#: Folder names never searched: they hold tooling or generated output, not a quest's work.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "dist", "build", "ai_driven", "human_context"})
#: How many folder levels below the root are searched (area/quest is two).
MAX_DEPTH = 3


class QuestFolderBindError(RuntimeError):
    """A bind that was refused (conflict, outside the root, bad input). Nothing was written."""


@dataclass
class FolderCandidate:
    path: str
    area: str                 # the folder's parent, relative to the root ("" when directly under it)
    markers: List[str] = field(default_factory=list)
    bound_to: Optional[str] = None   # the quest this folder already belongs to, if any

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "area": self.area, "markers": self.markers, "bound_to": self.bound_to}


@dataclass
class BindResult:
    quest_id: str
    folder: str
    map_file: str
    added: bool               # False when the pair was already recorded
    sync: Optional[QuestFolderSyncResult] = None


def resolve_root(new_quest_folder_root: Optional[str], corpus_root: Optional[str]) -> Optional[str]:
    """Where a new quest folder may be created: the configured root, else ``<corpus_root>/stories``, else None."""
    if new_quest_folder_root:
        return str(Path(new_quest_folder_root).expanduser())
    return str(Path(corpus_root).expanduser() / "stories") if corpus_root else None


def read_map(map_file: Optional[str]) -> Dict[str, str]:
    """The map file as ``{quest_id: folder}``. A missing or unreadable file is an empty map."""
    if not map_file:
        return {}
    try:
        data = json.loads(Path(map_file).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def folder_candidates(root: str, map_file: Optional[str] = None) -> List[FolderCandidate]:
    """Folders under ``root`` that look like a quest home, each marked with the quest it is bound to.

    READ ONLY. A folder qualifies when it holds ``CLAUDE.md``, ``QUEST_SYNC.md`` or ``GOALS.md``.
    ``bound_to`` comes from the map file, else from the quest id a ``QUEST_SYNC.md`` declares.
    """
    base = _resolve(root)
    if not base.is_dir():
        return []
    bound = {str(_resolve(v)): k for k, v in read_map(map_file).items()}
    found: List[FolderCandidate] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            markers = [m for m in HOME_MARKERS if (child / m).is_file()]
            if markers:
                owner = bound.get(str(child)) or quest_id_in_folder(str(child))
                parent = child.parent.relative_to(base).as_posix()
                found.append(FolderCandidate(str(child), "" if parent == "." else parent, markers, owner))
            if depth < MAX_DEPTH:
                walk(child, depth + 1)

    walk(base, 1)
    return found


def _merge_entry(map_file: Path, quest_id: str, folder: str) -> bool:
    """Add one entry under a file lock (temp file + rename). True when the file changed."""
    map_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = map_file.with_name(map_file.name + ".lock")
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current: Dict[str, Any] = {}
            if map_file.exists():
                try:
                    loaded = json.loads(map_file.read_text(encoding="utf-8") or "{}")
                except ValueError as e:
                    raise QuestFolderBindError(f"{map_file} is not valid JSON ({e}); not touching it") from e
                if not isinstance(loaded, dict):
                    raise QuestFolderBindError(f"{map_file} must hold a JSON object; not touching it")
                current = loaded
            for other, existing in current.items():
                if _resolve(str(existing)) == _resolve(folder) and str(other) != quest_id:
                    raise QuestFolderBindError(f"{folder} is already bound to quest {other}")
            if quest_id in current:
                if _resolve(str(current[quest_id])) == _resolve(folder):
                    return False
                raise QuestFolderBindError(
                    f"quest {quest_id} is already bound to {current[quest_id]}; "
                    f"the map file is changed by hand to move it")
            current[quest_id] = folder
            fd, tmp = tempfile.mkstemp(prefix=map_file.name + ".", dir=str(map_file.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(current, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(tmp, map_file)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def bind_quest_folder(map_file: str, quest_id: str, folder: str, *, root: str,
                      client: Any = None, direction: str = "pull",
                      quest_name: Optional[str] = None) -> BindResult:
    """Bind ``quest_id`` to ``folder``: record it in ``map_file``, create it, scaffold the zones, sync.

    ``root`` is the lane's ``new_quest_folder_root``; ``folder`` must resolve inside it. The folder
    is created when missing and may be an existing one. With ``client=None`` the Quest sync is
    skipped (the entry and the folder are still written). Raises :class:`QuestFolderBindError`, with
    nothing written, on a conflict or a path outside the root.
    """
    if not (map_file and quest_id and folder and root):
        raise QuestFolderBindError("map_file, quest_id, folder and root are all required")
    target = _resolve(folder)
    if not _inside(_resolve(root), target) or target == _resolve(root):
        raise QuestFolderBindError(f"{folder} is not inside {root}; a lane writes only inside its own root")
    owner = quest_id_in_folder(str(target)) if target.is_dir() else None
    if owner and owner != quest_id:
        raise QuestFolderBindError(f"{folder} already belongs to quest {owner}")
    added = _merge_entry(Path(map_file).expanduser(), quest_id, str(target))   # refuses before anything is created
    target.mkdir(parents=True, exist_ok=True)
    ensure_folder_zones(str(target), quest_id=quest_id, quest_name=quest_name)
    synced = None
    if client is not None:
        synced = sync_quest_folder(client, quest_id, str(target), direction=direction)
    log.info("bound quest %s to %s (%s)", quest_id, target, "new entry" if added else "already bound")
    return BindResult(quest_id, str(target), str(Path(map_file).expanduser()), added, synced)
