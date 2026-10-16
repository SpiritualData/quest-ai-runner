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
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.adapters import Observation  # noqa: F401  (re-exported type for callers building tests)
from ._walk import prune_dirnames, skip_subtrees
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

# Server-only secret rules, on top of files_adapter._is_secretish. Directory names whose whole
# subtree is refused, file names refused outright, suffixes refused, and name fragments refused
# when the file is a DATA file (a ``token_store.py`` is code and stays readable; ``api_token.json``
# is not).
SECRET_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".kube", ".azure", ".gcloud", ".docker", ".password-store",
})
SECRET_FILE_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "_netrc", ".npmrc", ".pypirc",
    ".htpasswd", ".git-credentials", "authorized_keys", "known_hosts", "shadow", "kubeconfig",
})
SECRET_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".gpg", ".asc", ".ovpn",
})
SECRET_DATA_FRAGMENTS = ("token", "apikey", "api_key", "api-key", "private_key", "private-key",
                         "service_account", "service-account", "oauth", "auth_key", "session_key")
DATA_SUFFIXES = frozenset({"", ".json", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
                           ".dat", ".pickle", ".pkl", ".csv", ".log"})

EXTRACTABLE_SUFFIXES = (".pdf", ".docx", ".xlsx")

# Secret VALUES. Each pattern is deliberately narrow (a long, key-shaped value) so ordinary code and
# prose are left alone; redaction replaces only the value, never the surrounding line.
REDACTED = "[redacted]"
REDACTION_PATTERNS: Tuple["re.Pattern[str]", ...] = tuple(re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"\bsk-[A-Za-z0-9_\-]{20,}",
    r"\bqsk_[A-Za-z0-9]{16,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}",
))
ASSIGNED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|passwd|token|authorization)(\W{1,4})([A-Za-z0-9_\-/+=]{20,})")
CREDENTIAL_SNIFF = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|"private_key"\s*:|"client_secret"\s*:')


def redact(text: str) -> str:
    """Replace obvious secret values in ``text`` with ``[redacted]``. Never raises."""
    if not text:
        return text
    out = text
    for pattern in REDACTION_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def name_is_secret(name: str) -> bool:
    """Whether one path COMPONENT is refused: the shared rule plus the server-only rules."""
    low = name.lower()
    if _is_secretish(name):
        return True
    if low in SECRET_DIR_NAMES or low in SECRET_FILE_NAMES:
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


def format_mtime(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class ServedFilesAdapter(FilesAdapter):
    """``FilesAdapter`` whose notion of a readable file also applies the server-only rules.

    ``FilesAdapter.grep_pass`` filters every candidate through ``_readable``, so overriding it here
    is what makes the search honour the stricter secret rules and the symlink fence without
    forking the walk."""

    def __init__(self, root: str, *, deny: Sequence[str] = (), **kwargs: Any):
        super().__init__(root, **kwargs)
        self.deny = normalize_deny(deny)
        self.denied_paths = [self.root / d for d in self.deny]

    def visible(self, path: Path) -> bool:
        """Secret rules and the symlink fence, without the text-only restriction (a pdf is visible)."""
        try:
            if path.is_symlink():
                real = path.resolve()
                real.relative_to(self.root)
                if name_is_secret(real.name):
                    return False
            rel = path.relative_to(self.root) if path.is_absolute() else path
            return not path_is_secret(rel.parts) and not is_denied(rel.as_posix(), self.deny)
        except (OSError, ValueError):
            return False

    def _readable(self, path: Path) -> bool:
        return super()._readable(path) and self.visible(path)


class FileOperations:
    """The four read operations over one root. Pure logic, no HTTP: the handler is a thin wrapper
    and the tests drive this directly."""

    def __init__(self, root: str, *, deny: Sequence[str] = (),
                 clock: Callable[[], float] = time.monotonic):
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError("the served root is not a directory")
        self.root = resolved
        self.files = ServedFilesAdapter(str(resolved), deny=deny, default_grep_max_hits=DEFAULT_MAX_HITS)
        self.clock = clock

    # --- helpers -------------------------------------------------------------

    def locate(self, rel: Optional[str]) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve a root-relative path. Returns ``(path, None)`` or ``(None, error)``."""
        text = (rel or "").strip().replace("\\", "/")
        if text in ("", ".", "./", "/"):
            return self.root, None
        resolved = resolve_in_tree(self.root, text)
        if resolved is None:
            return None, "path is outside the served root or not allowed"
        try:
            parts = resolved.relative_to(self.root).parts
        except ValueError:
            return None, "path is outside the served root or not allowed"
        if path_is_secret(parts) or is_denied("/".join(parts), self.files.deny):
            return None, "path is outside the served root or not allowed"
        return resolved, None

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
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        start, error = self.locate(scope)
        if start is None:
            return {"ok": False, "error": error}
        if not start.exists():
            return {"ok": False, "error": "scope does not exist"}
        limit = self.clamp(max_hits, DEFAULT_MAX_HITS, MAX_MAX_HITS)
        deadline = self.clock() + grep_budget_seconds()
        stats: Dict[str, Any] = {"files": 0, "stopped": ""}
        with skip_subtrees(self.files.denied_paths):
            raw_hits = self.files.grep_pass(start, rx, limit, deadline, stats)
        hits: List[Dict[str, Any]] = []
        seen_hits: set = set()
        for h in raw_hits:  # a symlink inside the root resolves to its target: report each line once
            key = (h["rel_path"], h["line_no"])
            if key not in seen_hits:
                seen_hits.add(key)
                hits.append({"path": h["rel_path"], "line": h["line_no"], "text": redact(h["line"])})
        note = ""
        if stats["stopped"]:
            note = (f"Search stopped early ({stats['stopped']}) after {stats['files']} files; results "
                    "may be incomplete. Narrow the scope to search the rest.")
        return {"ok": True, "hits": hits, "files_searched": stats["files"], "note": note}

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
            return {"ok": False, "error": "scope does not exist"}
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
            dirnames[:] = [d for d in dirnames if not name_is_secret(d)
                           and not is_denied(self.relative(Path(dirpath) / d), self.files.deny)]
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
            return {"ok": False, "error": "not a directory"}
        entries: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(target), key=str.lower)
        except OSError as exc:
            return {"ok": False, "error": f"could not list the directory: {type(exc).__name__}"}
        for name in names:
            if name_is_secret(name) or is_denied(self.relative(target / name), self.files.deny):
                continue
            child = target / name
            try:
                real = child.resolve()
                real.relative_to(self.root)
                if name_is_secret(real.name):
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
            return {"ok": False, "error": "not a file"}
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
                return {"ok": False, "error": "path is outside the served root or not allowed"}

        lines = text.splitlines()
        if heading:
            section = FilesAdapter._heading_section(lines, heading)
            if section is None:
                return {"ok": False, "error": f"heading not found: {heading!r}"}
            locator, chunk = f"heading={heading!r}", section["text"]
        elif start_line is not None or end_line is not None:
            first = max(1, int(start_line)) if start_line is not None else 1
            last = int(end_line) if end_line is not None else len(lines)
            last = min(max(last, first), len(lines))
            locator, chunk = f"lines {first}-{last}", "\n".join(lines[first - 1:last])
        else:
            locator, chunk = "head", text
        chunk = redact(chunk)
        encoded = chunk.encode("utf-8", errors="replace")
        truncated = len(encoded) > budget
        if truncated:
            chunk = encoded[:budget].decode("utf-8", errors="ignore").rstrip() + "\n...[truncated]"
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
    """A threaded HTTP server with a deeper accept queue than the stdlib default of 5."""
    request_queue_size = 32
    daemon_threads = True


def build_server(root: str, token: str, *, host: str = "127.0.0.1", port: int = 0,
                 deny: Sequence[str] = ()) -> ThreadingHTTPServer:
    """Build (not start) the server. Raises ``ValueError`` without a usable token or a real root.

    ``deny``: root-relative paths that are invisible to every operation (``QAR_FILES_DENY``).

    ``port=0`` picks a free port (tests); read it back from ``server.server_address``."""
    secret = (token or "").strip()
    if len(secret) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"a bearer token of at least {MIN_TOKEN_LENGTH} characters is required; "
            "refusing to start an open file server")
    handler = type("BoundRequestHandler", (RequestHandler,), {
        "operations": FileOperations(root, deny=deny),
        "token": secret.encode("utf-8"),
        "gate": threading.BoundedSemaphore(MAX_CONCURRENT_OPERATIONS),
    })
    server = FilesHTTPServer((host, port), handler)
    return server


def serve(root: str, token: str, *, host: str = "127.0.0.1", port: int = 8765,
          deny: Sequence[str] = ()) -> None:
    """Run the server until interrupted."""
    server = build_server(root, token, host=host, port=port, deny=deny)
    log.info("serving read-only files on %s:%s", *server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    finally:
        server.server_close()
