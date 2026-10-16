"""FilesServer: a small READ-ONLY file server a team runs next to its own files.

Quest AI (or any QAR brain) normally reads files through ``FilesAdapter``, which needs the files on
the same machine. When the brain runs somewhere else (a hosted backend), the team runs this server
on the machine that HAS the files and the brain reaches it with ``RemoteFilesAdapter``
(``adapters/remote_files_adapter.py``). Start it with ``quest-ai-runner serve-files`` (see
``docs/file-server.md``).

Why a plain JSON-over-HTTP API and not MCP (decision recorded here so it is not re-litigated):
the planner's own read vocabulary is ``grep(pattern, scope)`` and ``read_section(path, lines|heading)``,
which ``MCPRetrievalAdapter`` cannot offer (it refuses grep and reads only listed resources), and an
MCP server would make every file host install the pinned ``mcp`` package and its starlette pin. This
module is standard library only (``http.server``), so the host needs nothing beyond the package.

Operations (all ``POST /v1/<op>`` with a JSON body, bearer token required; ``GET /v1/health``
answers without one and reveals nothing):

  * ``search``  ``{pattern, scope?, max_hits?}``      regex over file contents
  * ``find``    ``{pattern, scope?, max_results?}``   files by name or glob, newest first
  * ``list``    ``{path?}``                           one directory
  * ``read``    ``{path, start_line?, end_line?, heading?, max_bytes?}``  text, or text extracted
                from pdf/docx/xlsx; any other binary file is answered with an honest note

Every answer is ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.

SAFETY (the point of the module, so each rule is enforced in one place and tested):

  * Read-only by construction: there is no write, delete, rename or execute code path.
  * Containment is ``files_adapter.resolve_in_tree``, the same function the rest of the library
    uses: ``..`` and absolute escapes fail, and a symlink that leads outside the root fails because
    resolution follows the link before the check. Directory walks never follow symlinked
    directories, and a symlinked FILE is only read when its real target is inside the root.
  * Secret files are refused by the shared ``_is_secretish`` rule (``.env*``, ``*.key``, ``*.pem``,
    names containing secret/credential/password) PLUS the stricter server-only rules below, applied
    to EVERY path component, so a file under ``.ssh/`` or ``credentials/`` is refused even when its
    own name looks harmless.
  * Obvious secret VALUES inside otherwise ordinary files (private key blocks, well-known API key
    shapes, bearer tokens, ``password = <long value>``) are replaced by ``[redacted]`` in every
    answer. This is a net, not a guarantee: keep real secrets out of the served tree.
  * The bearer token is required to start the server and compared in constant time. The server
    binds loopback by default; put TLS in front of it (a reverse proxy) before exposing it.
  * An operator can fence off parts of the tree entirely (``deny``, ``QAR_FILES_DENY``): a denied
    folder or file is invisible exactly like a secret one.
  * Bounded work: request body, response size, file size, search time and file count, and the number
    of operations running at once, all have caps.
"""
from __future__ import annotations

import fnmatch
import hmac
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.adapters import Observation  # noqa: F401  (re-exported type for callers building tests)
from ._walk import prune_dirnames
from .files_redaction import REDACTED, redact  # noqa: F401  (redact re-exported for callers and tests)
from .files_adapter import (
    FilesAdapter,
    _BINARY_EXTS,
    _is_secretish,
    grep_budget_seconds,
    resolve_in_tree,
)

log = logging.getLogger("quest-ai-runner.files_server")

API_PREFIX = "/v1"
MIN_TOKEN_LENGTH = 16
MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_READ_BYTES = 40_000
MAX_READ_BYTES = 400_000
MAX_FILE_BYTES = 20 * 1024 * 1024          # a bigger text file is searched, never loaded whole
MAX_EXTRACT_BYTES = 30 * 1024 * 1024       # pdf/docx/xlsx download cap before extraction
DEFAULT_MAX_HITS = 60
MAX_MAX_HITS = 300
DEFAULT_MAX_RESULTS = 100
MAX_MAX_RESULTS = 500
MAX_LIST_ENTRIES = 500
MAX_PATTERN_CHARS = 500
MAX_CONCURRENT_OPERATIONS = 4
MAX_CONNECTIONS = 32                       # open sockets (threads) at once; more get an instant 503
REQUEST_TIMEOUT_SECONDS = 30               # a client that stalls mid-request is dropped
SEARCH_KILL_GRACE_SECONDS = 3.0            # the search child is killed this long after its own budget
MAX_LINE_CHARS = 4000                      # a longer line is matched only on its first characters
MAX_ZIP_ENTRIES = 5000                     # docx/xlsx are zip files: refuse a zip bomb
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
REDACTION_MARGIN_BYTES = 4096
NOT_ALLOWED = "path is outside the served root or not allowed"   # ONE message for denied, secret and missing

# Server-only secret rules, on top of files_adapter._is_secretish. Directory names whose whole
# subtree is refused, file names refused outright, suffixes refused, and name fragments refused
# when the file is a DATA file (a ``token_store.py`` is code and stays readable; ``api_token.json``
# is not).
SECRET_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".kube", ".azure", ".gcloud", ".docker", ".password-store", "snap",
})
SECRET_FILE_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "_netrc", ".npmrc", ".pypirc",
    ".htpasswd", ".git-credentials", "authorized_keys", "known_hosts", "shadow", "kubeconfig",
    ".viminfo", ".lesshst", ".python_history", ".bash_history", ".zsh_history",
})
SECRET_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".gpg", ".asc", ".ovpn",
})
SECRET_DATA_FRAGMENTS = ("token", "apikey", "api_key", "api-key", "private_key", "private-key",
                         "service_account", "service-account", "oauth", "auth_key", "session_key")
DATA_SUFFIXES = frozenset({"", ".json", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
                           ".dat", ".pickle", ".pkl", ".csv", ".log"})

EXTRACTABLE_SUFFIXES = (".pdf", ".docx", ".xlsx")

CREDENTIAL_SNIFF = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|"private_key"\s*:|"client_secret"\s*:')


def name_is_secret(name: str) -> bool:
    """Whether one path COMPONENT is refused: the shared rule plus the server-only rules."""
    low = name.lower()
    if _is_secretish(name):
        return True
    if low in SECRET_DIR_NAMES or low in SECRET_FILE_NAMES:
        return True
    if low.endswith(("_history", ".history")):       # shell, repl and database client histories
        return True
    suffix = Path(low).suffix
    if suffix in SECRET_SUFFIXES:
        return True
    if suffix in DATA_SUFFIXES and any(frag in low for frag in SECRET_DATA_FRAGMENTS):
        return True
    return False


def path_is_secret(rel_parts: Tuple[str, ...]) -> bool:
    """Whether ANY component of a root-relative path is refused."""
    return any(name_is_secret(part) for part in rel_parts if part and part != ".")


def normalize_deny(entries: Sequence[str]) -> Tuple[str, ...]:
    """Root-relative paths an operator fenced off, cleaned (``./a//b/`` becomes ``a/b``)."""
    cleaned: List[str] = []
    for entry in entries or ():
        text = "/".join(part for part in str(entry).strip().replace("\\", "/").split("/")
                        if part not in ("", "."))
        if text and ".." not in text.split("/"):
            cleaned.append(text)
    return tuple(cleaned)


def is_denied(rel_posix: str, deny: Sequence[str]) -> bool:
    """Whether a root-relative path is one of the denied paths or lies inside one."""
    return any(rel_posix == d or rel_posix.startswith(d + "/") for d in deny)


def hidden_component(part: str) -> bool:
    return part.startswith(".") and part not in (".", "..")


def parse_allow_dot(entries: Sequence[str]) -> Tuple[Tuple[str, ...], ...]:
    """``QAR_FILES_ALLOW_DOT``: root-relative dot paths the operator opens up, as component tuples."""
    return tuple(tuple(e.split("/")) for e in normalize_deny(entries))


def dot_allowed(rel_parts: Sequence[str], allow: Sequence[Tuple[str, ...]]) -> bool:
    """A path with a dot-named component is served only when an allowlist entry (matched on WHOLE
    components, as a prefix) covers every dot-named component of it. No dot component: always fine."""
    dots = [i for i, part in enumerate(rel_parts) if hidden_component(part)]
    if not dots:
        return True
    last = dots[-1]
    return any(len(a) > last and tuple(rel_parts[:len(a)]) == a for a in allow)


def format_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class ServedFilesAdapter(FilesAdapter):
    """``FilesAdapter`` whose notion of a readable file also applies the server-only rules.

    ``FilesAdapter.grep_pass`` filters every candidate through ``_readable``, so overriding it here
    is what makes the search honour the stricter secret rules and the symlink fence without
    forking the walk."""

    grep_max_line_chars = MAX_LINE_CHARS

    def __init__(self, root: str, *, deny: Sequence[str] = (), allow_dot: Sequence[str] = (),
                 **kwargs: Any):
        super().__init__(root, **kwargs)
        self.deny = normalize_deny(deny)
        self.allow_dot = parse_allow_dot(allow_dot)
        self.denied_paths = [self.root / d for d in self.deny]

    def blocked(self, rel_parts: Sequence[str]) -> bool:
        """Whether a root-relative path is refused: secret component, dot component not on the
        operator allowlist, or inside a denied path."""
        parts = tuple(p for p in rel_parts if p and p != ".")
        return (path_is_secret(parts) or not dot_allowed(parts, self.allow_dot)
                or is_denied("/".join(parts), self.deny))

    def format_hit_line(self, line: str) -> str:
        return redact(line)

    def visible(self, path: Path) -> bool:
        """Every rule on BOTH the path as walked and, for a symlink, the real file it resolves to
        (without the text-only restriction: a pdf is visible)."""
        try:
            rel = path.relative_to(self.root) if path.is_absolute() else path
            if self.blocked(rel.parts):
                return False
            if path.is_symlink():
                real_rel = path.resolve().relative_to(self.root)
                if self.blocked(real_rel.parts):
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _readable(self, path: Path) -> bool:
        return super()._readable(path) and self.visible(path)


def zip_is_reasonable(data: bytes) -> bool:
    """docx and xlsx are zip files: refuse one that declares thousands of entries or an enormous
    unpacked size (a zip bomb), before any extractor inflates it."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            return len(infos) <= MAX_ZIP_ENTRIES and sum(i.file_size for i in infos) <= MAX_UNZIPPED_BYTES
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


class FileOperations:
    """The four read operations over one root. Pure logic, no HTTP: the handler is a thin wrapper
    and the tests drive this directly."""

    def __init__(self, root: str, *, deny: Sequence[str] = (), allow_dot: Sequence[str] = (),
                 clock: Callable[[], float] = time.monotonic):
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError("the served root is not a directory")
        self.root = resolved
        self.deny_entries = list(deny or ())
        self.allow_dot_entries = list(allow_dot or ())
        self.files = ServedFilesAdapter(str(resolved), deny=deny, allow_dot=allow_dot,
                                        default_grep_max_hits=DEFAULT_MAX_HITS)
        self.clock = clock

    # --- helpers -------------------------------------------------------------

    def locate(self, rel: Optional[str]) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve a root-relative path. Returns ``(path, None)`` or ``(None, error)``.

        The rules run on the path as typed AND on where it really leads (a symlink), and every
        refusal carries the same message, so a caller cannot tell a denied path from a missing one."""
        text = (rel or "").strip().replace("\\", "/")
        if text in ("", ".", "./", "/"):
            return self.root, None
        resolved = resolve_in_tree(self.root, text)
        if resolved is None:
            return None, NOT_ALLOWED
        try:
            real_parts = resolved.relative_to(self.root).parts
            if text.startswith("/"):
                typed_parts = Path(text).relative_to(self.root).parts
            else:
                typed_parts = tuple(p for p in text.split("/") if p not in ("", "."))
        except ValueError:
            return None, NOT_ALLOWED
        if self.files.blocked(real_parts) or self.files.blocked(typed_parts):
            return None, NOT_ALLOWED
        return resolved, None

    def rel_parts(self, path: Path) -> Tuple[str, ...]:
        try:
            return path.relative_to(self.root).parts
        except ValueError:
            return (path.name,)

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            return path.name

    @staticmethod
    def clamp(value: Any, default: int, upper: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(number, upper)) if number > 0 else default

    # --- search ----------------------------------------------------------------

    def search(self, pattern: str, scope: Optional[str] = None,
               max_hits: Optional[int] = None) -> Dict[str, Any]:
        pattern = (pattern or "").strip()
        if not pattern:
            return {"ok": False, "error": "pattern is required"}
        if len(pattern) > MAX_PATTERN_CHARS:
            return {"ok": False, "error": f"pattern is longer than {MAX_PATTERN_CHARS} characters"}
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        start, error = self.locate(scope)
        if start is None:
            return {"ok": False, "error": error}
        if not start.exists():
            return {"ok": False, "error": NOT_ALLOWED}
        limit = self.clamp(max_hits, DEFAULT_MAX_HITS, MAX_MAX_HITS)
        raw_hits, stats = self.run_search_child(start, pattern, limit)
        hits: List[Dict[str, Any]] = []
        seen_hits: set = set()
        for h in raw_hits:  # a symlink inside the root resolves to its target: report each line once
            key = (h["rel_path"], h["line_no"])
            if key not in seen_hits:
                seen_hits.add(key)
                hits.append({"path": h["rel_path"], "line": h["line_no"], "text": h["line"]})
        note = ""
        if stats["stopped"]:
            note = (f"Search stopped early ({stats['stopped']}) after {stats['files']} files; results "
                    "may be incomplete. Narrow the scope to search the rest.")
        return {"ok": True, "hits": hits, "files_searched": stats["files"], "note": note}

    def run_search_child(self, start: Path, pattern: str,
                         limit: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Run the walk-and-match in a child process that is KILLED at the budget.

        A user-supplied regex can take exponential time on one line (``(a|aa)+$X``), and no check
        between files can interrupt that, while the work also holds the GIL and would freeze the
        whole server. A child process cannot be reasoned with, only killed: the parent stays
        responsive, keeps the hits the child had already printed, and reports the search as stopped."""
        budget = grep_budget_seconds()
        request = json.dumps({
            "root": str(self.root), "deny": self.deny_entries, "allow_dot": self.allow_dot_entries,
            "start": str(start), "pattern": pattern, "limit": limit, "budget": budget,
            "max_files": os.getenv("QAR_GREP_MAX_FILES", ""), "skip_dirs": os.getenv("QAR_SKIP_DIRS", ""),
        }).encode("utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
               "PYTHONIOENCODING": "utf-8", "LC_ALL": "C.UTF-8"}
        stats: Dict[str, Any] = {"files": 0, "stopped": ""}
        hits: List[Dict[str, Any]] = []
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "quest_ai_runner.adapters.files_search_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, close_fds=True, start_new_session=True)
        except OSError as exc:
            log.warning("could not start the search child: %s", type(exc).__name__)
            stats["stopped"] = "search unavailable"
            return hits, stats
        killed = False
        try:
            out, _ = child.communicate(request, timeout=budget + SEARCH_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            out, _ = child.communicate()
            killed = True
        finished = False
        for raw in (out or b"").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if "hit" in row:
                hits.append(row["hit"])
            elif row.get("done"):
                finished = True
                stats["files"], stats["stopped"] = int(row.get("files", 0)), str(row.get("stopped", ""))
        if killed:
            stats["stopped"] = "time budget (the pattern may be too complex)"
        elif not finished:
            stats["stopped"] = "search failed"
        return hits, stats

    # --- find ------------------------------------------------------------------

    def find(self, pattern: str, scope: Optional[str] = None,
             max_results: Optional[int] = None) -> Dict[str, Any]:
        pattern = (pattern or "").strip()
        if not pattern:
            return {"ok": False, "error": "pattern is required"}
        start, error = self.locate(scope)
        if start is None:
            return {"ok": False, "error": error}
        if not start.exists():
            return {"ok": False, "error": NOT_ALLOWED}
        limit = self.clamp(max_results, DEFAULT_MAX_RESULTS, MAX_MAX_RESULTS)
        has_glob = any(ch in pattern for ch in "*?[")
        wants_path = "/" in pattern
        needle = pattern.lower()
        deadline = self.clock() + grep_budget_seconds()
        matches: List[Tuple[float, int, str]] = []
        seen_files = 0
        stopped = ""
        collect_cap = MAX_MAX_RESULTS * 4

        def accepts(rel: str, name: str) -> bool:
            target = rel if wants_path else name
            if has_glob:
                return fnmatch.fnmatch(target.lower(), needle)
            return needle in target.lower()

        if start.is_file():
            walk = [(str(start.parent), [], [start.name])]
        else:
            walk = os.walk(start)
        for dirpath, dirnames, filenames in walk:
            prune_dirnames(dirnames, current=Path(dirpath), base_skip=self.files.skip_dirs)
            dirnames[:] = [d for d in dirnames
                           if not self.files.blocked(self.rel_parts(Path(dirpath) / d))]
            rel_dir = self.relative(Path(dirpath))
            for fn in filenames:
                if fn.startswith(".") and not start.is_file():
                    continue
                if self.clock() > deadline:
                    stopped = "time budget"
                    break
                seen_files += 1
                rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                if not accepts(rel, fn):      # the cheap name test first; the filesystem checks only for matches
                    continue
                path = Path(dirpath) / fn
                if not self.files.visible(path):
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue
                matches.append((info.st_mtime, info.st_size, rel))
                if len(matches) >= collect_cap:
                    stopped = "result cap"
                    break
            if stopped:
                break
        matches.sort(key=lambda m: m[0], reverse=True)
        results = [{"path": rel, "size": size, "modified": format_mtime(mtime)}
                   for mtime, size, rel in matches[:limit]]
        note = ""
        if stopped:
            note = f"Search stopped early ({stopped}); results may be incomplete. Narrow the scope."
        elif len(matches) > limit:
            note = f"{len(matches)} files matched; showing the newest {limit}."
        return {"ok": True, "results": results, "files_seen": seen_files, "note": note}

    # --- list ------------------------------------------------------------------

    def list_dir(self, path: Optional[str] = None) -> Dict[str, Any]:
        target, error = self.locate(path)
        if target is None:
            return {"ok": False, "error": error}
        if not target.is_dir():
            return {"ok": False, "error": NOT_ALLOWED}
        entries: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(target), key=str.lower)
        except OSError as exc:
            return {"ok": False, "error": f"could not list the directory: {type(exc).__name__}"}
        for name in names:
            child = target / name
            if self.files.blocked(self.rel_parts(child)):
                continue
            try:
                real = child.resolve()
                real.relative_to(self.root)
                if self.files.blocked(self.rel_parts(real)):
                    continue
                info = real.stat()
            except (OSError, ValueError):
                continue
            kind = "dir" if real.is_dir() else "file"
            entries.append({"name": name, "type": kind, "size": 0 if kind == "dir" else info.st_size,
                            "modified": format_mtime(info.st_mtime)})
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        note = ""
        if len(entries) > MAX_LIST_ENTRIES:
            note = f"{len(entries)} entries; showing the first {MAX_LIST_ENTRIES}."
            entries = entries[:MAX_LIST_ENTRIES]
        return {"ok": True, "path": self.relative(target), "entries": entries, "note": note}

    # --- read ------------------------------------------------------------------

    def read(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None,
             heading: Optional[str] = None, max_bytes: Optional[int] = None) -> Dict[str, Any]:
        target, error = self.locate(path)
        if target is None:
            return {"ok": False, "error": error}
        if not target.is_file():
            return {"ok": False, "error": NOT_ALLOWED}
        rel = self.relative(target)
        budget = self.clamp(max_bytes, DEFAULT_READ_BYTES, MAX_READ_BYTES)
        suffix = target.suffix.lower()
        try:
            size = target.stat().st_size
        except OSError as exc:
            return {"ok": False, "error": f"could not read the file: {type(exc).__name__}"}

        if suffix in EXTRACTABLE_SUFFIXES:
            kind, text, note = self.extract(target, suffix, size)
            if text is None:
                return {"ok": True, "path": rel, "kind": "binary", "text": note, "truncated": False,
                        "locator": "note"}
        elif suffix in _BINARY_EXTS:
            return {"ok": True, "path": rel, "kind": "binary", "locator": "note", "truncated": False,
                    "text": f"{rel} is a binary file ({suffix.lstrip('.')}, {size} bytes); "
                            "its content cannot be read as text."}
        else:
            if size > MAX_FILE_BYTES:
                return {"ok": False, "error": f"file is {size} bytes; too large to read whole. "
                                              "Search it or ask for a narrower file."}
            try:
                data = target.read_bytes()
            except OSError as exc:
                return {"ok": False, "error": f"could not read the file: {type(exc).__name__}"}
            if b"\x00" in data[:4096]:
                return {"ok": True, "path": rel, "kind": "binary", "locator": "note",
                        "truncated": False,
                        "text": f"{rel} is a binary file ({size} bytes); its content cannot be read as text."}
            text = data.decode("utf-8", errors="replace")
            kind = "text"
            if CREDENTIAL_SNIFF.search(text[:200_000]):
                return {"ok": False, "error": NOT_ALLOWED}

        lines = text.splitlines()
        if heading:
            section = FilesAdapter._heading_section(lines, heading)
            if section is None:
                titles = [ln.strip() for ln in lines if ln.lstrip().startswith("#")][:25]
                hint = (" Headings in this file: " + " | ".join(t[:80] for t in titles)
                        + ". Use one exactly, or read without a heading.") if titles else (
                    " This file has no headings; read it by lines or without a heading.")
                return {"ok": False, "error": f"heading not found: {heading!r}.{hint}"}
            locator, chunk = f"heading={heading!r}", section["text"]
        elif start_line is not None or end_line is not None:
            first = max(1, int(start_line)) if start_line is not None else 1
            last = int(end_line) if end_line is not None else len(lines)
            last = min(max(last, first), len(lines))
            locator, chunk = f"lines {first}-{last}", "\n".join(lines[first - 1:last])
        else:
            locator, chunk = "head", text
        encoded = chunk.encode("utf-8", errors="replace")
        truncated = len(encoded) > budget
        if truncated:
            # Redact only the part that will be returned (plus a margin so a secret straddling the
            # cap is seen whole), never the whole file; then cut to the cap.
            window = encoded[:budget + REDACTION_MARGIN_BYTES].decode("utf-8", errors="ignore")
            if "\n" in window:
                window = window.rsplit("\n", 1)[0]
            cut = redact(window).encode("utf-8", errors="replace")[:budget]
            chunk = cut.decode("utf-8", errors="ignore").rstrip() + "\n...[truncated]"
        else:
            chunk = redact(chunk)
        return {"ok": True, "path": rel, "kind": kind, "locator": locator, "text": chunk,
                "total_lines": len(lines), "truncated": truncated}

    @staticmethod
    def extract(path: Path, suffix: str, size: int) -> Tuple[str, Optional[str], str]:
        """Text from a pdf/docx/xlsx via the library's own attachment extractors.

        Returns ``(kind, text, note)``; ``text`` is None when nothing could be extracted and ``note``
        then says why (honest, never empty)."""
        label = suffix.lstrip(".")
        if size > MAX_EXTRACT_BYTES:
            return label, None, f"{path.name} is a {label} file of {size} bytes, too large to extract."
        try:
            data = path.read_bytes()
        except OSError as exc:
            return label, None, f"could not read {path.name}: {type(exc).__name__}"
        if suffix in (".docx", ".xlsx") and not zip_is_reasonable(data):
            return label, None, f"{path.name} is a {label} file with an unusual internal structure; not extracted."
        from ..core import attachments
        extractor = {".pdf": attachments._extract_pdf, ".docx": attachments._extract_docx,
                     ".xlsx": attachments._extract_xlsx}[suffix]
        text = extractor(data)
        if not text:
            return label, None, (f"{path.name} is a {label} file ({size} bytes) but no text could be "
                                 "extracted (scanned image, encrypted, or the reader is not installed).")
        return label, text, ""


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP wrapper around ``FileOperations``. Class attributes are bound by ``build_server``."""

    operations: FileOperations
    token: bytes
    gate: threading.BoundedSemaphore
    server_version = "qar-files"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS      # a stalled client is dropped, not held forever

    def log_message(self, fmt: str, *args: Any) -> None:  # route access logs through logging
        log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(supplied.strip().encode("utf-8"), self.token)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.split("?", 1)[0] == f"{API_PREFIX}/health":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        route = self.path.split("?", 1)[0]
        if not route.startswith(API_PREFIX + "/"):
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        op = route[len(API_PREFIX) + 1:]
        if op not in ("search", "find", "list", "read"):
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.send_json(413, {"ok": False, "error": "request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except ValueError:
            self.send_json(400, {"ok": False, "error": "body must be a JSON object"})
            return
        if not self.gate.acquire(blocking=False):
            self.send_json(503, {"ok": False, "error": "server is busy; retry shortly"})
            return
        try:
            result = self.dispatch(op, body)
        except Exception as exc:  # noqa: BLE001 - an operation must never kill the server
            log.warning("operation %s failed: %s", op, type(exc).__name__)
            result = {"ok": False, "error": f"operation failed: {type(exc).__name__}"}
        finally:
            self.gate.release()
        self.send_json(200, result)

    def dispatch(self, op: str, body: Dict[str, Any]) -> Dict[str, Any]:
        ops = self.operations
        if op == "search":
            return ops.search(str(body.get("pattern") or ""), body.get("scope"), body.get("max_hits"))
        if op == "find":
            return ops.find(str(body.get("pattern") or ""), body.get("scope"), body.get("max_results"))
        if op == "list":
            return ops.list_dir(body.get("path"))
        return ops.read(str(body.get("path") or ""), body.get("start_line"), body.get("end_line"),
                        body.get("heading"), body.get("max_bytes"))


class FilesHTTPServer(ThreadingHTTPServer):
    """A threaded HTTP server with a deeper accept queue than the stdlib default of 5 and a cap on
    open connections (one thread each): past the cap a client gets an instant 503."""
    request_queue_size = 32
    daemon_threads = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.connection_slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.connection_slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


def build_server(root: str, token: str, *, host: str = "127.0.0.1", port: int = 0,
                 deny: Sequence[str] = (), allow_dot: Sequence[str] = ()) -> ThreadingHTTPServer:
    """Build (not start) the server. Raises ``ValueError`` without a usable token or a real root.

    ``deny``: root-relative paths that are invisible to every operation (``QAR_FILES_DENY``).

    ``allow_dot``: root-relative dot paths that stay readable (``QAR_FILES_ALLOW_DOT``); every other
    path with a dot-named component is refused.

    ``port=0`` picks a free port (tests); read it back from ``server.server_address``."""
    secret = (token or "").strip()
    if len(secret) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"a bearer token of at least {MIN_TOKEN_LENGTH} characters is required; "
            "refusing to start an open file server")
    handler = type("BoundRequestHandler", (RequestHandler,), {
        "operations": FileOperations(root, deny=deny, allow_dot=allow_dot),
        "token": secret.encode("utf-8"),
        "gate": threading.BoundedSemaphore(MAX_CONCURRENT_OPERATIONS),
    })
    server = FilesHTTPServer((host, port), handler)
    return server


def serve(root: str, token: str, *, host: str = "127.0.0.1", port: int = 8765,
          deny: Sequence[str] = (), allow_dot: Sequence[str] = ()) -> None:
    """Run the server until interrupted."""
    server = build_server(root, token, host=host, port=port, deny=deny, allow_dot=allow_dot)
    log.info("serving read-only files on %s:%s", *server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    finally:
        server.server_close()
