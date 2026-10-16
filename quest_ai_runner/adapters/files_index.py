"""Visible-path index for the file server: "closest paths" on a miss, and a compact top-level map.

Both features are computed ONLY from paths the server's own visibility filter lets through (the
same ``blocked`` rule ``list`` and ``find`` apply), so neither can name a denied, secret or dot
path. A miss answers with the one identical refusal whether the path is denied, secret or simply
absent; the suggestions are a pure function of the typed path and the visible set, so they carry
no information about which of the three it was. A suggestion equal to the typed path is dropped.

The index is built lazily by one walk, kept for a short time (``ttl``), and rebuilt on demand, so a
70k file tree costs one walk per few minutes and a miss costs a dictionary lookup.
"""
from __future__ import annotations

import difflib
import functools
import heapq
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ._walk import prune_dirnames

DEFAULT_TTL_SECONDS = 120.0
MAX_INDEX_ENTRIES = 400_000
BUILD_BUDGET_SECONDS = 10.0
MAX_SUGGESTIONS = 5
MAP_MAX_LINES = 60
MAP_LINE_CHARS = 200
MAP_ROOT_FILE_NAMES = 12
MAP_CHILD_DIRS = 8
MAP_MAX_CHARS = 2800


def clean_parts(text: Optional[str]) -> List[str]:
    """The usable components of a typed path (no empty, ``.`` or ``..`` parts, no drive/root)."""
    raw = (text or "").replace("\\", "/").split("/")
    return [p for p in raw if p not in ("", ".", "..")]


class VisibleIndex:
    """Every visible file and folder under the root, as root-relative posix paths."""

    def __init__(self, operations, *, ttl: float = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.operations = operations
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.Lock()
        self.built_at: Optional[float] = None
        self.files: List[str] = []
        self.dirs: List[str] = []
        self.files_by_name: Dict[str, List[str]] = {}
        self.dirs_by_name: Dict[str, List[str]] = {}

    # --- build ---------------------------------------------------------------------

    def fresh(self) -> bool:
        return self.built_at is not None and (self.clock() - self.built_at) < self.ttl

    def ensure(self) -> None:
        """A fresh index is used as is. A stale one is used as is too while a background rebuild
        runs (a miss never waits for a walk after the first). Only the very first call waits."""
        if self.fresh():
            return
        if self.built_at is not None:
            if self.lock.acquire(blocking=False):
                threading.Thread(target=self.rebuild_and_release, daemon=True).start()
            return
        with self.lock:
            if self.built_at is None:
                self.build()

    def rebuild_and_release(self) -> None:
        try:
            self.build()
        except Exception:  # noqa: BLE001 - the old index stays in use
            pass
        finally:
            self.lock.release()

    def warm(self) -> None:
        """Build the first index off the request path (called when the server starts)."""
        if self.lock.acquire(blocking=False):
            threading.Thread(target=self.warm_build, daemon=True).start()

    def warm_build(self) -> None:
        try:
            if self.built_at is None:
                self.build()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.lock.release()

    def build(self) -> None:
        """One walk. A folder that passed the visibility rules makes its children cheap to judge:
        only each child's own name, the dot allowlist for a dot name, and the deny list remain."""
        from .files_server import dot_allowed, hidden_component, is_denied, name_is_secret  # no import cycle at load
        ops = self.operations
        adapter = ops.files
        deadline = self.clock() + BUILD_BUDGET_SECONDS
        files: List[str] = []
        dirs: List[str] = []
        secret = functools.lru_cache(maxsize=65536)(name_is_secret)
        deny, allow = adapter.deny, adapter.allow_dot

        def child_blocked(parent: Tuple[str, ...], name: str) -> bool:
            if secret(name):
                return True
            if hidden_component(name) and not dot_allowed(parent + (name,), allow):
                return True
            return bool(deny) and is_denied("/".join(parent + (name,)), deny)

        for dirpath, dirnames, filenames in os.walk(ops.root):
            prune_dirnames(dirnames, current=Path(dirpath), base_skip=adapter.skip_dirs)
            rel_dir = ops.relative(Path(dirpath))
            parent = () if rel_dir == "." else tuple(rel_dir.split("/"))
            dirnames[:] = [d for d in dirnames if not child_blocked(parent, d)]
            prefix = "" if not parent else rel_dir + "/"
            dirs.extend(prefix + d for d in dirnames)
            files.extend(prefix + fn for fn in filenames if not child_blocked(parent, fn))
            if len(files) + len(dirs) >= MAX_INDEX_ENTRIES or self.clock() > deadline:
                break
        self.files, self.dirs = files, dirs
        self.files_by_name = self.group(files)
        self.dirs_by_name = self.group(dirs)
        self.built_at = self.clock()

    @staticmethod
    def group(paths: Sequence[str]) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for path in paths:
            grouped.setdefault(path.rsplit("/", 1)[-1].lower(), []).append(path)
        return grouped

    # --- suggestions -----------------------------------------------------------------

    @staticmethod
    def trailing_match(candidate: str, wanted: Sequence[str]) -> int:
        """How many trailing components of ``candidate`` equal the end of ``wanted`` (case blind)."""
        have = candidate.lower().split("/")
        count = 0
        for a, b in zip(reversed(have), reversed(wanted)):
            if a != b:
                break
            count += 1
        return count

    def suggest(self, typed: Optional[str], limit: int = MAX_SUGGESTIONS) -> List[str]:
        """Up to ``limit`` visible paths closest to ``typed``: the same file or folder under another
        top folder first, then the same name, then a near spelling. Folders end with ``/``."""
        parts = clean_parts(typed)
        if not parts:
            return []
        try:
            self.ensure()
        except OSError:
            return []
        wanted = [p.lower() for p in parts]
        typed_norm = "/".join(wanted)
        scored: Dict[str, int] = {}

        def add(path: str, score: int, is_dir: bool) -> None:
            key = path + "/" if is_dir else path
            if path.lower() == typed_norm:
                return
            scored[key] = max(scored.get(key, 0), score - path.count("/"))

        last = wanted[-1]
        for path in self.files_by_name.get(last, ()):
            add(path, 1000 + 100 * self.trailing_match(path, wanted), False)
        stem = last.rsplit(".", 1)[0] if "." in last.strip(".") else last
        if not scored and len(stem) >= 3:
            for name, paths in self.files_by_name.items():
                if name.rsplit(".", 1)[0] == stem:
                    for path in paths:
                        add(path, 700 + 100 * self.trailing_match(path, wanted), False)
        for k in range(len(wanted), 0, -1):
            prefix = wanted[:k]
            for path in self.dirs_by_name.get(prefix[-1], ()):
                if self.trailing_match(path, prefix) == k:
                    add(path, 500 + 100 * k, True)
        if not any(not key.endswith("/") for key in scored):
            near = [n for n in self.files_by_name if abs(len(n) - len(last)) <= 3]
            near += [n for n in self.dirs_by_name if abs(len(n) - len(last)) <= 3]
            for name in difflib.get_close_matches(last, near, n=3, cutoff=0.75):
                for path in self.files_by_name.get(name, ())[:3]:
                    add(path, 300, False)
                for path in self.dirs_by_name.get(name, ())[:3]:
                    add(path, 300, True)
        ranked = heapq.nlargest(limit * 3, scored.items(), key=lambda kv: (kv[1], -len(kv[0])))
        out: List[str] = []
        adapter, ops = self.operations.files, self.operations
        for path, _score in ranked:
            real = Path(ops.root) / path.rstrip("/")
            if adapter.blocked(ops.rel_parts(real)) or not adapter.visible(real):
                continue          # belt and braces: the symlink-aware check, on the few we show
            out.append(path)
            if len(out) >= limit:
                break
        return out

    # --- top-level map -----------------------------------------------------------------

    @staticmethod
    def quiet_last(name: str) -> int:
        low = name.lower()
        return 1 if any(word in low for word in ("archive", "backup", "old", "tmp", "cache")) else 0

    def tree_map(self) -> str:
        """A compact picture of the top of the tree: root files, then each root folder with its
        most recently changed subfolders (archive-like names last). Visible entries only, within
        ``MAP_MAX_LINES`` lines and ``MAP_MAX_CHARS`` characters."""
        ops = self.operations
        top = ops.list_dir(None)
        if not top.get("ok"):
            return ""
        entries = top.get("entries") or []
        root_files = [e["name"] for e in entries if e.get("type") != "dir"]
        root_dirs = [e["name"] for e in entries if e.get("type") == "dir"]
        skip = ops.files.skip_dirs
        listed = {}
        for name in root_dirs[:MAP_MAX_LINES - 2]:
            sub = ops.list_dir(name)
            kids = (sub.get("entries") or []) if sub.get("ok") else []
            subdirs = [e for e in kids if e.get("type") == "dir" and e["name"] not in skip
                       and not e["name"].startswith("__")]
            subdirs.sort(key=lambda e: (self.quiet_last(e["name"]), _neg(e.get("modified"))))
            listed[name] = (subdirs, sum(1 for e in kids if e.get("type") != "dir"))
        text = ""
        for per_dir in (MAP_CHILD_DIRS, 5, 3, 0):
            text = self.render_map(root_files, root_dirs, listed, per_dir)
            if len(text) <= MAP_MAX_CHARS:
                break
        return text

    def render_map(self, root_files, root_dirs, listed, per_dir: int) -> str:
        lines: List[str] = []
        if root_files:
            shown = ", ".join(root_files[:MAP_ROOT_FILE_NAMES])
            more = len(root_files) - MAP_ROOT_FILE_NAMES
            lines.append(f"(top folder files) {shown}" + (f", +{more} more" if more > 0 else ""))
        for name in root_dirs[:MAP_MAX_LINES - len(lines) - 1]:
            subdirs, nfiles = listed.get(name, ([], 0))
            parts = [e["name"] + "/" for e in subdirs[:per_dir]]
            if len(subdirs) > per_dir:
                parts.append(f"+{len(subdirs) - per_dir} more folders")
            if nfiles:
                parts.append(f"{nfiles} file{'s' if nfiles != 1 else ''}")
            lines.append((f"{name}/: " + ", ".join(parts))[:MAP_LINE_CHARS] if parts else f"{name}/")
        hidden = len(root_dirs) - (len(lines) - (1 if root_files else 0))
        if hidden > 0:
            lines.append(f"(+{hidden} more top folders: list the top folder)")
        return "\n".join(lines)


def _neg(modified: Optional[str]) -> Tuple[int, ...]:
    """Sort key putting the newest ``YYYY-MM-DD HH:MM`` first."""
    digits = "".join(ch for ch in (modified or "") if ch.isdigit())
    return tuple(-int(ch) for ch in digits)
