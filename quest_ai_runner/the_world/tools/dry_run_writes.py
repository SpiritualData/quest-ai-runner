"""Dry-run writes: run the real brain against real data with every state change intercepted and recorded, never executed.

Opt in with ``QAR_DRY_RUN_WRITES=1``. Unset (the default) nothing here does anything: every function returns at once and no hook changes a
call. Set, one process runs the unchanged production code path (config, context, personas, tools, model) while:

* the Quest client refuses every request that is not a read with an explicit 403 "blocked in test mode" after recording it (``quest_exchange``),
  so nothing reaches Quest and nothing can read as done;
* a tool whose spec says it ``mutates`` is not run (``tool_call``): the call and its arguments are recorded and the tool reports "Blocked in
  test mode", so a reply cannot claim the action was made;
* the deep worker is replaced by a stub (``DryDeepRunner``) that records the brief it would have been given and reports that the run did not start (blocked in test mode, a failed run with nothing left to try, so the brain keeps the answer it already has when the run was its own escalation);
* every other way out of the process is closed by ``install``: an HTTP request whose method is not GET, HEAD or OPTIONS is refused unless
  it is on the explicit read-only list below, a file opened for writing is redirected to a scratch copy, a file removal or rename is
  refused, and a subprocess is refused unless it is the model CLI answering with no tools.

A run may also be BOUNDED: ``set_allowances`` names the few writes the case asks for (a tool and its arguments, or a Quest method and path),
and exactly those run for real, in the thread of the allowed call; every other write is still refused and recorded. The allowed ones are listed
in ``RECORDER.performed``. No allowance is ever read from the model's words: they come from the case.

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
import logging
import threading
import time
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
#: git commands that only read the repository (the harness records the commit of the code it ran).
READ_PROCESSES = frozenset({"uname", "hostname", "whoami", "nproc", "getconf", "lsb_release"})   # report the machine, change nothing
READ_GIT = frozenset({"rev-parse", "status", "diff", "log", "show", "rev-list", "ls-files", "describe", "config"})
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
        self.done: List[str] = []
        self.model_log: List[Dict[str, Any]] = []
        self.skipped_housekeeping = 0
        self.created_goals: set = set()
        self.call_titles: set = set()
        self.unexpected: List[str] = []

    def created_goal(self, goal_id: str) -> None:
        """A goal this run created: it may be changed again by the same run."""
        with self.lock:
            self.created_goals.add(str(goal_id))

    def call_title(self, title: str) -> None:
        with self.lock:
            self.call_titles.add(str(title).strip().casefold())

    def clear(self) -> None:
        with self.lock:
            self.writes, self.reads, self.model_calls, self.briefs, self.missing_tools, self.done = [], Counter(), 0, [], [], []
            self.model_log, self.skipped_housekeeping = [], 0
            self.created_goals, self.call_titles, self.unexpected = set(), set(), []

    def missing_tool(self, name: str) -> None:
        """The model asked for a tool this deployment does not have: a gap in the lane, reported apart from a model failure."""
        with self.lock:
            self.missing_tools.append(str(name))

    def write(self, kind: str, what: str, internal: bool = False, **detail: Any) -> None:
        """``internal`` marks the runner's own bookkeeping (its card store, its vector index, its state files), which is
        listed apart from the writes a person's request caused."""
        with self.lock:
            self.writes.append({"kind": kind, "what": what, "internal": internal, **detail})

    def performed(self, what: str) -> None:
        with self.lock:
            self.done.append(what)

    def read(self, name: str) -> None:
        with self.lock:
            self.reads[name] += 1

    def model_call(self, label: str = "") -> None:
        """One allowed model process, with what it was for (the start of its system prompt) so a count can be broken down by purpose."""
        with self.lock:
            self.model_calls += 1
            self.model_log.append({"t": time.time(), "label": label})

    def skipped(self, what: str) -> None:
        """A housekeeping write the guard stopped, counted once here instead of reported where the caller logged its failure."""
        with self.lock:
            self.skipped_housekeeping += 1

    def lines(self, internal: bool = False) -> List[str]:
        """The ``would have done`` lines of the writes a request caused (``internal=True``: the runner's own bookkeeping instead)."""
        with self.lock:
            return [f"would have done: {w['kind']}: {w['what']}" for w in self.writes if bool(w.get("internal")) == internal]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"writes": list(self.writes), "reads": dict(self.reads), "model_calls": self.model_calls,
                    "briefs": list(self.briefs), "missing_tools": list(self.missing_tools), "performed": list(self.done),
                    "model_log": list(self.model_log), "skipped_housekeeping": self.skipped_housekeeping, "unexpected": list(self.unexpected)}


RECORDER = Recorder()
STATE: Dict[str, Any] = {"installed": False, "scratch": None, "originals": {}, "allowances": [], "harness_paths": [], "read_commands": set()}
LOCAL = threading.local()


def set_allowances(rules: Optional[List[Mapping[str, Any]]], harness_paths: Optional[List[str]] = None) -> None:
    """The writes this run may really make. A rule is ``{"tool": name, "args": {k: v}}`` (the tool, when every listed argument is equal; a key ending in ``~`` means "starts with"; ``a.b`` names key ``b`` inside the object argument ``a``) or
    ``{"method": "POST", "path": "<regex>"}`` (a Quest request). ``harness_paths`` are folders the measuring harness writes its own record to."""
    STATE["allowances"] = [dict(r) for r in rules or []]
    STATE["harness_paths"] = [str(p) for p in harness_paths or [] if p]


def argument_at(args: Mapping[str, Any], dotted: str) -> Any:
    """An argument by name; ``change.kind`` reads the key ``kind`` inside the object argument ``change`` (None when absent)."""
    value: Any = args
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


def argument_matches(args: Mapping[str, Any], key: str, want: Any) -> bool:
    """One argument test of a rule: equal to the value, or (a key ending in ``~``) starting with it. A list of values means any of them."""
    prefix = key.endswith("~")
    have = str(argument_at(args, key[:-1] if prefix else key)).strip().lower()
    for value in (want if isinstance(want, list) else [want]):
        value = str(value).strip().lower()
        if (have.startswith(value) if prefix else have == value):
            return True
    return False


def tool_allowed(name: str, args: Mapping[str, Any]) -> bool:
    for rule in STATE["allowances"]:
        if rule.get("tool") == name and all(argument_matches(args, k, v) for k, v in (rule.get("args") or {}).items()):
            return True
    return False


UNREADABLE = "UNREADABLE:"          # a refusal that is a failed read of the goal, not a write to a goal the case may not change
GOAL_WRITE = re.compile(r"^/api/planning/goals/([^/?]+)(/assign)?$")


def set_goal_title_lookup(lookup: Optional[Any]) -> None:
    """``lookup(goal_id) -> current title or None``: how the guard below reads a goal before a change to it is allowed (the harness gives it a fresh read of the quest's goals)."""
    STATE["goal_title_lookup"] = lookup


def title_prefixes() -> List[str]:
    """The title starts the case's rules allow a goal to have when it is changed (the ``title~`` / ``title`` argument of an allowed tool call)."""
    out: List[str] = []
    for rule in STATE["allowances"]:
        for key, want in (rule.get("args") or {}).items():
            if key.rstrip("~") == "title":
                out += [str(w).strip().casefold() for w in (want if isinstance(want, list) else [want])]
    return out


def goal_write_refusal(method: str, path: str) -> Optional[str]:
    """Why a PUT to a goal (or its /assign) must not go even inside an allowed tool call, else None. The target's CURRENT title and description are read first. It may be changed only when this
    run created it, or its title starts with a title the case's rule allows (a rule with no title constraint allows creating a goal, never updating one), or its title ends in the keyed
    "next step: week of YYYY-MM-DD" pattern, or its description begins with the marker Quest AI writes on the goals it keeps. A title that merely equals the allowed call's title is no
    reason: that is how a person's goal would be changed by a call carrying its own title."""
    found = GOAL_WRITE.match(path.split("?")[0])
    if method.upper() != "PUT" or not found:
        return None
    goal_id = found.group(1)
    if goal_id in RECORDER.created_goals:
        return None
    lookup = STATE.get("goal_title_lookup")
    goal = None
    if lookup is not None:
        try:
            goal = lookup(goal_id)
        except Exception:  # noqa: BLE001 -- a goal that cannot be read is not changed
            goal = None
    if goal is None:
        return f"{UNREADABLE} goal {goal_id}: its current title could not be read, so it is not changed"
    title = str(goal.get("title") if isinstance(goal, Mapping) else goal)
    description = str(goal.get("description") or "") if isinstance(goal, Mapping) else ""
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
    current = title.strip().casefold()
    if is_owned({"name": title, "description": description}) or any(prefix and current.startswith(prefix) for prefix in title_prefixes()):
        return None
    return f"goal {goal_id} ({title[:80]!r}) is not a goal this case may change"


def request_allowed(method: str, path: str) -> bool:
    return any(rule.get("method", "").upper() == method.upper() and re.match(rule.get("path", "") + r"\Z", path.split("?")[0])
               for rule in STATE["allowances"] if rule.get("path"))


class allow_scope:
    """Inside it (in this thread) the writes of an allowed tool call go through. ``allowed`` False makes it do nothing."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def __enter__(self):
        if self.allowed:
            LOCAL.depth = getattr(LOCAL, "depth", 0) + 1
        return self

    def __exit__(self, *exc) -> None:
        if self.allowed:
            LOCAL.depth -= 1


def in_allowed_scope() -> bool:
    return getattr(LOCAL, "depth", 0) > 0


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
    """None when the Quest request may go (a read or an allowed write); a write that is not allowed is recorded and refused with an explicit
    403 "blocked in test mode", never a stand-in success: a reply must not be able to claim a blocked write was made."""
    if not active() or method.upper() in SAFE_METHODS:
        if active():
            RECORDER.read(f"quest {path.split('?')[0]}")
        return None
    if in_allowed_scope() or request_allowed(method, path):
        refusal = goal_write_refusal(method, path)
        if refusal:
            if not refusal.startswith(UNREADABLE):          # only a write to a goal the case may not change is an unexpected write; a goal that could not be read is a lane gap, not a reason to stop
                line = f"UNEXPECTED WRITE blocked: {method.upper()} {path.split('?')[0]}: {refusal}"
                with RECORDER.lock:
                    RECORDER.unexpected.append(line)
            RECORDER.write("quest api", f"{method.upper()} {path.split('?')[0]} ({'blocked' if refusal.startswith(UNREADABLE) else 'unexpected'}: {refusal})")
            from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError
            raise QuestApiError(f"Quest API {method.upper()} {path.split('?')[0]} -> 403: blocked in test mode, nothing was changed ({refusal})", status=403, code="blocked_in_test_mode")
        RECORDER.performed(f"quest api {method.upper()} {path.split('?')[0]}")
        return None
    RECORDER.write("quest api", f"{method.upper()} {path.split('?')[0]} {summarize(body) if body else ''}".strip(), internal=is_internal(path))
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError
    raise QuestApiError(f"Quest API {method.upper()} {path.split('?')[0]} -> 403: blocked in test mode, nothing was changed", status=403, code="blocked_in_test_mode")


# --- tools --------------------------------------------------------------------------------------

def tool_call(spec: Any, name: str, args: Mapping[str, Any]):
    """None when the tool may run, else a refused ``ToolResult`` ("Blocked in test mode") for a tool that mutates, after recording the call."""
    if not active():
        return None
    from quest_ai_runner.the_world.tools.tools_status import ToolStatus
    from quest_ai_runner.the_world.tools.tools import ToolResult
    if not getattr(spec, "mutates", True):
        RECORDER.read(f"tool {name}")
        return None
    if tool_allowed(name, args):
        RECORDER.performed(f"tool {name} {summarize(dict(args))}")
        if str(args.get("title") or "").strip():
            RECORDER.call_title(str(args["title"]))
        return None
    unbound = [arg for arg, key in (getattr(spec, "context_args", None) or {}).items() if key == "quest_id" and not args.get(arg)]
    if unbound:     # the call names no quest: the real tool would say so and the planner would find the quest, so test mode says the same instead of "blocked"
        return ToolResult(ok=False, status=ToolStatus.REFUSED, text=(f"{name}: no quest is known for this call. Call again with {unbound[0]} set to the id of the quest the "
                                                                    "request is about, taken from the quests listed in context (find_quest lists them)."))
    RECORDER.write("tool", f"{name} {summarize(dict(args))}", tool=name)
    return ToolResult(ok=False, status=ToolStatus.REFUSED, text=f"Blocked in test mode: {name} was not run and nothing was changed.")


# --- the deep worker ----------------------------------------------------------------------------

class DryDeepRunner:
    """Stands in for every deep worker: records the goal and brief it would have been given and reports that the run was not started (blocked in test mode)."""

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
        return DeepResult(met=False, exhausted=True, error="blocked_in_test_mode",
                          output="Blocked in test mode: the background run was not started and nothing was changed.")

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
    """A path under the scratch folder, the system temp folder or a harness folder (its own record of the run) may be written."""
    if in_allowed_scope():
        return True
    for root in (scratch_dir(), Path(tempfile.gettempdir()), *(Path(p) for p in STATE["harness_paths"])):
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def check_request(method: str, url: str) -> None:
    """Raise ``DryRunBlocked`` (after recording) for a request that is not a read or an allowed read-only POST."""
    method = (method or "GET").upper()
    if in_allowed_scope():
        return
    parsed = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    if method in SAFE_METHODS or is_read_post(parsed.hostname or "", parsed.path):
        RECORDER.read(f"http {parsed.hostname}")
        return
    RECORDER.write("http", f"{method} {parsed.hostname}{parsed.path}", internal=is_internal(parsed.path))
    raise DryRunBlocked(f"dry run: {method} {parsed.hostname}{parsed.path} was not sent")


def declare_read_command(argv: Any) -> None:
    """Let exactly this command line start in a dry run: the lane's configuration declared it a read (a metric source). Any other process stays refused."""
    STATE["read_commands"].add(tuple(str(a) for a in argv))


def check_process(argv: Any) -> None:
    parts = [str(a) for a in (argv if isinstance(argv, (list, tuple)) else [argv])]
    if tuple(parts) in STATE["read_commands"]:
        RECORDER.read("metric source")
        return
    if parts and os.path.basename(parts[0]) in READ_PROCESSES:
        return
    if parts and os.path.basename(parts[0]) == "git":
        rest = parts[1:]
        while rest[:1] in (["-C"], ["-c"]):
            rest = rest[2:]
        if rest[:1] and rest[0] in READ_GIT and not (rest[0] == "config" and any(a for a in rest[1:] if not a.startswith("--get"))):
            return
    names = {os.path.basename(p) for p in parts[:6]}
    if names & MODEL_CLI_NAMES:
        no_tools = "--tools" in parts and parts[parts.index("--tools") + 1: parts.index("--tools") + 2] == [""]
        harmless = parts[-1] in ("--help", "--version")
        if (no_tools or harmless) and not any(flag in parts for flag in MODEL_CLI_FORBIDDEN):
            if not harmless:
                prompt = next((parts[i + 1] for i, a in enumerate(parts[:-1]) if a in ("--system-prompt", "--append-system-prompt")), "")
                RECORDER.model_call(f"{caller_label()} | {' '.join(prompt.split())[:50]}")
            return
    RECORDER.write("process", summarize(" ".join(parts), 200))
    raise DryRunBlocked(f"dry run: process {summarize(' '.join(parts), 80)} was not started")


PLUMBING = ("vendor_model_caller", "dry_run_writes", "model_registry", "model_tier", "subprocess.py", "threading.py", "provider", "multi_")


def caller_label(depth: int = 3) -> str:
    """The first ``depth`` functions of the call stack that are not model plumbing, as ``module.function``: what asked for a model call."""
    import sys
    frame, found = sys._getframe(2), []
    while frame is not None and len(found) < depth:
        path = frame.f_code.co_filename
        if not any(mark in path for mark in PLUMBING) and ("quest_ai_runner" in path or "hubofhubs" in path):
            found.append(f"{Path(path).stem}.{frame.f_code.co_name}")
        frame = frame.f_back
    return " < ".join(found) or "unknown"


def chain_has_block(error: Optional[BaseException]) -> bool:
    """True when ``error`` or anything it was raised from or while handling is a ``DryRunBlocked``."""
    seen = 0
    while error is not None and seen < 12:
        if isinstance(error, DryRunBlocked):
            return True
        error, seen = error.__cause__ or error.__context__, seen + 1
    return False


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
            return None                    # the runner's own housekeeping outside the scratch folder: skipped quietly, counted once
        setattr(os, name, guarded)
    for name in ("remove", "unlink", "rename", "replace", "rmdir", "mkdir", "removedirs"):
        refuse_path_change(name)
    shutil_rmtree = originals["shutil.rmtree"] = shutil.rmtree
    def rmtree_guard(path, *args, **kwargs):
        if writable_here(Path(os.fspath(path)).absolute()):
            return shutil_rmtree(path, *args, **kwargs)
        RECORDER.write("file", f"rmtree {path}", internal=True)
        return None
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

    log_handle = originals["log_handle"] = logging.Logger.handle
    def quiet_handle(self, record):
        if record.exc_info and chain_has_block(record.exc_info[1]) or "DryRunBlocked" in str(record.msg) + " ".join(map(str, record.args or ())):
            RECORDER.skipped("housekeeping write skipped")
            return None
        return log_handle(self, record)
    logging.Logger.handle = quiet_handle

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
    logging.Logger.handle = originals["log_handle"]
    try:
        import httpx
        httpx.Client.send, httpx.AsyncClient.send = originals["httpx.Client"], originals["httpx.AsyncClient"]
    except (ImportError, KeyError):
        pass
    STATE["installed"], STATE["originals"] = False, {}
