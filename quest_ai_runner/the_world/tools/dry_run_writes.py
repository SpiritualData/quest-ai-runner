"""Dry-run writes: run the real brain against real data with every state change intercepted and recorded, never executed.

Opt in with ``QAR_DRY_RUN_WRITES=1``. Unset (the default) nothing here does anything: every function returns at once and no hook changes a
call. Set, one process runs the unchanged production code path (config, context, personas, tools, model) while:

* the Quest client answers every request that is not a read with a recorded stand-in (``quest_exchange``), so a tool that creates a task
  or a note sees a success and nothing reaches Quest;
* a tool whose spec says it ``mutates`` is not run (``tool_call``): the call and its arguments are recorded and the tool reports done;
* the deep worker is replaced by a stub (``DryDeepRunner``) that records the brief it would have been given and reports the run as queued;
* every other way out of the process is closed by ``install``: an HTTP request whose method is not GET, HEAD or OPTIONS is refused unless
  it is on the explicit read-only list below, a file opened for writing is redirected to a scratch copy, a file removal or rename is
  refused, and a subprocess is refused unless it is the model CLI answering with no tools.

Everything refused is listed in ``RECORDER.writes`` as ``would have done: <what>``; the reads that ran are counted in ``RECORDER.reads``.
Status, never the model's words, decides what is a read: the method of the request, the ``mutates`` flag of the spec, the argument list of
the process. The module imports nothing from the rest of the package at import time, so any layer may import it.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import os
import re
import tempfile
import threading
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

ENV = "QAR_DRY_RUN_WRITES"
SCRATCH_ENV = "QAR_DRY_RUN_SCRATCH"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

#: POST endpoints that only read, each checked against the code that sends it. (host pattern, path pattern); a bare path pattern
#: applies to every host in ``READ_POST_ANY_HOST``.
READ_POST_RULES = (
    # Notion: the adapter itself allows only this one POST (notion_adapter.QUERY_PATH), a database query.
    (r"api\.notion\.com", r"/v1/databases/[0-9a-fA-F-]+/query"),
    # Qdrant on this machine: the vector reads. Upserts are PUT and stay refused.
    (r"(localhost|127\.0\.0\.1|\[?::1\]?)", r"/collections/[^/]+/points/(search|query|scroll|count|recommend|search/batch|query/batch)"),
    # Embedding the query text (a pure function of the text, nothing stored).
    (r"api\.voyageai\.com", r"/v1/(embeddings|contextualizedembeddings)"),
    # OAuth token exchange for the read-only Google scopes the lane is configured with.
    (r"oauth2\.googleapis\.com", r"/token"),
    # The Drive MCP server, whose configured tool list is read-only (qar.toml allowed_tools).
    (r"drivemcp\.googleapis\.com", r"/mcp/v1/?"),
)
READ_POST_COMPILED = tuple((re.compile(h + r"\Z"), re.compile(p + r"\Z")) for h, p in READ_POST_RULES)
#: The model CLI answers with no tools in these flags (hubofhubs claude_cli), so it can change nothing.
MODEL_CLI_NAMES = frozenset({"claude"})
MODEL_CLI_FORBIDDEN = ("--dangerously-skip-permissions", "--allowedTools", "--allowed-tools", "--permission-mode")


class DryRunBlocked(PermissionError):
    """Raised where a refused request, file change or process has no stand-in to return."""


class Recorder:
    """What a dry run saw: the writes it refused (in order), the reads it counted and the model calls it allowed."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.writes: List[Dict[str, Any]] = []
        self.reads: Counter = Counter()
        self.model_calls = 0
        self.briefs: List[Dict[str, Any]] = []
        self.missing_tools: List[str] = []

    def clear(self) -> None:
        with self.lock:
            self.writes, self.reads, self.model_calls, self.briefs, self.missing_tools = [], Counter(), 0, [], []

    def missing_tool(self, name: str) -> None:
        """The model asked for a tool this deployment does not have: a gap in the lane, reported apart from a model failure."""
        with self.lock:
            self.missing_tools.append(str(name))

    def write(self, kind: str, what: str, internal: bool = False, **detail: Any) -> None:
        """``internal`` marks the runner's own bookkeeping (its card store, its vector index, its state files), which is
        listed apart from the writes a person's request caused."""
        with self.lock:
            self.writes.append({"kind": kind, "what": what, "internal": internal, **detail})

    def read(self, name: str) -> None:
        with self.lock:
            self.reads[name] += 1

    def model_call(self) -> None:
        with self.lock:
            self.model_calls += 1

    def lines(self, internal: bool = False) -> List[str]:
        """The ``would have done`` lines of the writes a request caused (``internal=True``: the runner's own bookkeeping instead)."""
        with self.lock:
            return [f"would have done: {w['kind']}: {w['what']}" for w in self.writes if bool(w.get("internal")) == internal]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"writes": list(self.writes), "reads": dict(self.reads), "model_calls": self.model_calls,
                    "briefs": list(self.briefs), "missing_tools": list(self.missing_tools)}


RECORDER = Recorder()
STATE: Dict[str, Any] = {"installed": False, "scratch": None, "originals": {}}


def active(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when ``QAR_DRY_RUN_WRITES`` is set to a truthy value."""
    raw = (os.environ if environ is None else environ).get(ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def summarize(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


# --- the Quest client ---------------------------------------------------------------------------

INTERNAL_PATHS = ("/api/cards", "/collections/")


def is_internal(path: str) -> bool:
    """True for a path the runner writes for its own memory: the card store and the vector index."""
    return (path or "").startswith(INTERNAL_PATHS)


def is_read_post(host: str, path: str) -> bool:
    host = (host or "").lower()
    return any(h.match(host) and p.match(path or "") for h, p in READ_POST_COMPILED)


def quest_exchange(method: str, path: str, body: Any = None):
    """None when the Quest request may go (a read), else ``(status, payload)`` to answer it with, after recording it."""
    if not active() or method.upper() in SAFE_METHODS:
        if active():
            RECORDER.read(f"quest {path.split('?')[0]}")
        return None
    RECORDER.write("quest api", f"{method.upper()} {path.split('?')[0]} {summarize(body) if body else ''}".strip(), internal=is_internal(path))
    stand_in = f"dryrun-{hashlib.sha1(f'{method} {path} {body}'.encode()).hexdigest()[:10]}"
    return 200, {"id": stand_in, "task_id": stand_in, "success": True, "applied": True, "dry_run": True}


# --- tools --------------------------------------------------------------------------------------

def tool_call(spec: Any, name: str, args: Mapping[str, Any]):
    """None when the tool may run, else a ``ToolResult`` standing in for a tool that mutates, after recording the call."""
    if not active():
        return None
    from quest_ai_runner.the_world.tools.tools_status import ToolStatus
    from quest_ai_runner.the_world.tools.tools import ToolResult
    if not getattr(spec, "mutates", True):
        RECORDER.read(f"tool {name}")
        return None
    RECORDER.write("tool", f"{name} {summarize(dict(args))}", tool=name)
    return ToolResult(ok=True, status=ToolStatus.APPLIED, text=f"{name} done.", data={"id": "dryrun", "dry_run": True})


# --- the deep worker ----------------------------------------------------------------------------

class DryDeepRunner:
    """Stands in for every deep worker: records the goal and brief it would have been given and reports the run as queued."""

    write_surface = "agent"

    def __init__(self, real: Any = None) -> None:
        self.real = real
        self.future_context_channel = getattr(real, "future_context_channel", "output")

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None, emit=None,
                 context_preamble: Optional[str] = None, run_id: Optional[str] = None, **extra: Any):
        from hubofhubs.api import DeepResult
        RECORDER.write("deep run", f"goal: {summarize(goal, 300)}", model=model, brief_chars=len(brief or ""))
        with RECORDER.lock:
            RECORDER.briefs.append({"goal": goal, "brief": brief, "model": model})
        return DeepResult(met=True, deferred=True, output="Queued: the work is running in the background and will report back here.")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "real":
            raise AttributeError(name)
        return getattr(self.real, name)


def wrap_deep(runner: Any) -> Any:
    """The stub for a deep runner (a dict or list of them keeps its shape), or the runner itself when a dry run is off or it is None."""
    if not active() or runner is None:
        return runner
    if isinstance(runner, dict):
        return {key: wrap_deep(value) for key, value in runner.items()}
    if isinstance(runner, (list, tuple)):
        return type(runner)(wrap_deep(value) for value in runner)
    return runner if isinstance(runner, DryDeepRunner) else DryDeepRunner(runner)


# --- everything else leaving the process ---------------------------------------------------------

def scratch_dir() -> Path:
    if STATE["scratch"] is None:
        root = os.environ.get(SCRATCH_ENV) or tempfile.mkdtemp(prefix="qar-dry-run-")
        STATE["scratch"] = Path(root)
        STATE["scratch"].mkdir(parents=True, exist_ok=True)
    return STATE["scratch"]


def writable_here(path: Path) -> bool:
    """A path under the scratch folder or the system temp folder may be written."""
    for root in (scratch_dir(), Path(tempfile.gettempdir())):
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def check_request(method: str, url: str) -> None:
    """Raise ``DryRunBlocked`` (after recording) for a request that is not a read or an allowed read-only POST."""
    method = (method or "GET").upper()
    parsed = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    if method in SAFE_METHODS or is_read_post(parsed.hostname or "", parsed.path):
        RECORDER.read(f"http {parsed.hostname}")
        return
    RECORDER.write("http", f"{method} {parsed.hostname}{parsed.path}", internal=is_internal(parsed.path))
    raise DryRunBlocked(f"dry run: {method} {parsed.hostname}{parsed.path} was not sent")


def check_process(argv: Any) -> None:
    parts = [str(a) for a in (argv if isinstance(argv, (list, tuple)) else [argv])]
    names = {os.path.basename(p) for p in parts[:6]}
    if names & MODEL_CLI_NAMES:
        no_tools = "--tools" in parts and parts[parts.index("--tools") + 1: parts.index("--tools") + 2] == [""]
        harmless = parts[-1] in ("--help", "--version")
        if (no_tools or harmless) and not any(flag in parts for flag in MODEL_CLI_FORBIDDEN):
            if not harmless:
                RECORDER.model_call()
            return
    RECORDER.write("process", summarize(" ".join(parts), 200))
    raise DryRunBlocked(f"dry run: process {summarize(' '.join(parts), 80)} was not started")


def install(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Close the process (once). Returns True when the guard is in place; False, doing nothing, when ``QAR_DRY_RUN_WRITES`` is unset."""
    if not active(environ):
        return False
    if STATE["installed"]:
        return True
    STATE["installed"] = True
    import http.client
    import shutil
    import subprocess
    import urllib.request
    scratch_dir()
    originals = STATE["originals"]

    open_real = originals["open"] = builtins.open
    def open_guard(file, mode="r", *args, **kwargs):
        if isinstance(file, int) or not any(flag in str(mode) for flag in "wax+"):
            return open_real(file, mode, *args, **kwargs)
        path = Path(os.fspath(file)).absolute()
        if writable_here(path):
            return open_real(file, mode, *args, **kwargs)
        RECORDER.write("file", f"write {path}", internal=True)
        stand_in = scratch_dir() / "redirected" / hashlib.sha1(str(path).encode()).hexdigest()
        stand_in.parent.mkdir(parents=True, exist_ok=True)
        if not stand_in.exists() and path.is_file() and ("a" in mode or "+" in mode):
            stand_in.write_bytes(path.read_bytes())
        return open_real(stand_in, mode, *args, **kwargs)
    builtins.open = open_guard
    io.open = open_guard

    def refuse_path_change(name):
        real = originals[f"os.{name}"] = getattr(os, name)
        def guarded(*args, **kwargs):
            paths = [Path(os.fspath(a)).absolute() for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))]
            if paths and all(writable_here(p) for p in paths):
                return real(*args, **kwargs)
            RECORDER.write("file", f"{name} {' '.join(str(p) for p in paths)}", internal=True)
            raise DryRunBlocked(f"dry run: os.{name} was not done")
        setattr(os, name, guarded)
    for name in ("remove", "unlink", "rename", "replace", "rmdir", "mkdir", "removedirs"):
        refuse_path_change(name)
    shutil_rmtree = originals["shutil.rmtree"] = shutil.rmtree
    def rmtree_guard(path, *args, **kwargs):
        if writable_here(Path(os.fspath(path)).absolute()):
            return shutil_rmtree(path, *args, **kwargs)
        RECORDER.write("file", f"rmtree {path}", internal=True)
        raise DryRunBlocked("dry run: rmtree was not done")
    shutil.rmtree = rmtree_guard

    opener_open = originals["opener"] = urllib.request.OpenerDirector.open
    def opener_guard(self, fullurl, data=None, timeout=None):
        if isinstance(fullurl, str):
            check_request("POST" if data is not None else "GET", fullurl)
        else:
            check_request(fullurl.get_method(), fullurl.full_url)
        return opener_open(self, fullurl, data, timeout) if timeout is not None else opener_open(self, fullurl, data)
    urllib.request.OpenerDirector.open = opener_guard

    conn_request = originals["conn"] = http.client.HTTPConnection.request
    def conn_guard(self, method, url, *args, **kwargs):
        check_request(method, f"{'https' if getattr(self, 'default_port', 80) == 443 else 'http'}://{self.host}:{self.port}{url}")
        return conn_request(self, method, url, *args, **kwargs)
    http.client.HTTPConnection.request = conn_guard

    try:
        import httpx
        for cls in (httpx.Client, httpx.AsyncClient):
            send = originals[f"httpx.{cls.__name__}"] = cls.send
            def make(send_real, is_async):
                if is_async:
                    async def guarded(self, request, *args, **kwargs):
                        check_request(request.method, str(request.url))
                        return await send_real(self, request, *args, **kwargs)
                else:
                    def guarded(self, request, *args, **kwargs):
                        check_request(request.method, str(request.url))
                        return send_real(self, request, *args, **kwargs)
                return guarded
            cls.send = make(send, cls is httpx.AsyncClient)
    except ImportError:
        pass

    popen_init = originals["popen"] = subprocess.Popen.__init__
    def popen_guard(self, args, *rest, **kwargs):
        check_process(args)
        return popen_init(self, args, *rest, **kwargs)
    subprocess.Popen.__init__ = popen_guard
    return True


def uninstall() -> None:
    """Put every patched function back (tests only)."""
    if not STATE["installed"]:
        return
    import http.client
    import shutil
    import subprocess
    import urllib.request
    originals = STATE["originals"]
    builtins.open = io.open = originals["open"]
    for name in ("remove", "unlink", "rename", "replace", "rmdir", "mkdir", "removedirs"):
        setattr(os, name, originals[f"os.{name}"])
    shutil.rmtree = originals["shutil.rmtree"]
    urllib.request.OpenerDirector.open = originals["opener"]
    http.client.HTTPConnection.request = originals["conn"]
    subprocess.Popen.__init__ = originals["popen"]
    try:
        import httpx
        httpx.Client.send, httpx.AsyncClient.send = originals["httpx.Client"], originals["httpx.AsyncClient"]
    except (ImportError, KeyError):
        pass
    STATE["installed"], STATE["originals"] = False, {}
