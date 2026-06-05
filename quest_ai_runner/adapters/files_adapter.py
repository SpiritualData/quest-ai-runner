"""FilesAdapter — read_section / grep over a CONFIGURED root (a RetrievalAdapter).

The hard read boundary is whatever root the CONSUMER passes — there is NO path baked in. Used
to ground on indexed files (an org's docs + corpus). Strictly read-only; everything is
hard-scoped inside the root,
skips secret-ish / binary / oversize files, and never shells out (pure ``re`` + ``os.walk``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".cache",
}
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mov",
    ".woff", ".woff2", ".ttf", ".ico", ".so", ".pyc", ".bin", ".db", ".sqlite",
}
_SECRET_HINTS = ("secret", "credential", "password")
_SECRET_SUFFIXES = (".env", ".key", ".pem")


def _is_secretish(name: str) -> bool:
    low = name.lower()
    if low.startswith(".env") or any(low.endswith(s) for s in _SECRET_SUFFIXES):
        return True
    return any(h in low for h in _SECRET_HINTS)


class FilesAdapter(RetrievalAdapterBase):
    def __init__(self, root: str, *, default_read_max_bytes: int = 20000,
                 default_grep_max_hits: int = 40, grep_max_file_bytes: int = 256 * 1024):
        self.root = Path(root).resolve()
        self.default_read_max_bytes = default_read_max_bytes
        self.default_grep_max_hits = default_grep_max_hits
        self.grep_max_file_bytes = grep_max_file_bytes

    # --- scope helpers -------------------------------------------------------

    def _resolve_in_tree(self, rel_or_abs: str) -> Optional[Path]:
        raw = (rel_or_abs or "").strip()
        if not raw:
            return None
        p = Path(raw)
        candidate = p if p.is_absolute() else (self.root / p)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.root)
        except (ValueError, OSError):
            return None
        if _is_secretish(resolved.name):
            return None
        return resolved

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return path.name

    def _readable(self, path: Path) -> bool:
        return not _is_secretish(path.name) and path.suffix.lower() not in _BINARY_EXTS

    @staticmethod
    def _heading_section(lines: List[str], heading: str):
        want = heading.strip().lower().lstrip("#").strip()
        if not want:
            return None

        def level(ln: str):
            m = re.match(r"^(#{1,6})\s+", ln)
            return len(m.group(1)) if m else None

        start_idx = None
        start_level = 0
        for i, ln in enumerate(lines):
            lvl = level(ln)
            if lvl is not None and want in ln.lstrip("#").strip().lower():
                start_idx, start_level = i, lvl
                break
        if start_idx is None:
            return None
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            lvl = level(lines[j])
            if lvl is not None and lvl <= start_level:
                end_idx = j
                break
        return {"start_line": start_idx + 1, "end_line": end_idx,
                "text": "\n".join(lines[start_idx:end_idx])}

    # --- RetrievalAdapter API ------------------------------------------------

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None,
                     max_bytes=None) -> Observation:
        max_bytes = max_bytes if (max_bytes and max_bytes > 0) else self.default_read_max_bytes
        resolved = self._resolve_in_tree(rel_path)
        if resolved is None:
            return Observation(kind="error", rel_path=rel_path, error="path outside root or not allowed")
        if not resolved.is_file():
            return Observation(kind="error", rel_path=rel_path, error="not a file")
        if not self._readable(resolved):
            return Observation(kind="error", rel_path=rel_path, error="binary/secret file not readable")
        rel = self._rel(resolved)
        try:
            raw = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return Observation(kind="error", rel_path=rel, error=f"read failed: {type(e).__name__}")

        lines = raw.splitlines()
        if heading:
            sec = self._heading_section(lines, heading)
            if sec is None:
                return Observation(kind="error", rel_path=rel, error=f"heading not found: {heading!r}")
            locator = f"heading={heading!r}"
            text = sec["text"]
        elif start_line is not None or end_line is not None:
            s = max(1, int(start_line)) if start_line is not None else 1
            e = int(end_line) if end_line is not None else len(lines)
            e = min(max(e, s), len(lines))
            locator = f"lines {s}-{e}"
            text = "\n".join(lines[s - 1:e])
        else:
            locator = "head"
            text = raw

        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > max_bytes:
            text = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip() + "\n…[truncated]"
        return Observation(kind="read", rel_path=rel, locator=locator, text=text)

    def grep(self, pattern, *, scope=None, max_hits=None) -> Observation:
        max_hits = max_hits if (max_hits and max_hits > 0) else self.default_grep_max_hits
        search_root = self.root
        if scope:
            resolved = self._resolve_in_tree(scope)
            if resolved is None:
                return Observation(kind="error", pattern=pattern, scope=scope, error="scope outside root")
            search_root = resolved
        if not search_root.exists():
            return Observation(kind="error", pattern=pattern, scope=scope, error="scope does not exist")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return Observation(kind="error", pattern=pattern, scope=scope, error=f"bad regex: {e}")

        files: List[Path] = []
        if search_root.is_file():
            files = [search_root]
        else:
            for dirpath, dirnames, filenames in os.walk(search_root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if not fn.startswith("."):
                        files.append(Path(dirpath) / fn)

        hits = []
        truncated = False
        for path in files:
            if truncated:
                break
            if not self._readable(path):
                continue
            try:
                if path.stat().st_size > self.grep_max_file_bytes:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, start=1):
                        if rx.search(line):
                            snippet = line.rstrip("\n")
                            if len(snippet) > 300:
                                snippet = snippet[:300].rstrip() + " …"
                            hits.append({"rel_path": self._rel(path), "line_no": line_no, "line": snippet})
                            if len(hits) >= max_hits:
                                truncated = True
                                break
            except OSError:
                continue
        return Observation(kind="grep", pattern=pattern, scope=scope, hits=hits)
