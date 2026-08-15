"""MCPClient -- a raw MCP (Model Context Protocol) protocol client. ZERO QAR semantics.

This is the low-level wire client: connect to one MCP server (stdio subprocess or streamable
HTTP), discover its tools/resources/prompts, call a tool, read a resource. It knows nothing about
``RetrievalAdapter``, allowlisting, namespacing, or truncation policy -- that mapping lives one
layer up, in ``mcp_retrieval_adapter.MCPRetrievalAdapter``. Keeping the two separate is the same
split this repo already draws between a raw protocol client and its QAR-facing adapter (compare
``acp_deep_runner.py``'s ACP session handling vs. what a ``DeepRunner`` exposes).

Pinned against **mcp==2.0.0** (see the ``[mcp]`` optional extra in ``pyproject.toml``). This was
the current release on PyPI at the time this module was written (Aug 2026) and reflects the MCP
protocol's July 2026 revision, so its API shapes (snake_case pydantic fields, ``ClientSession``,
``stdio_client``, ``streamable_http_client``) are what this module targets. If a consumer pins an
older ``mcp`` 1.x, field names differ in places (``.field_of`` below is deliberately tolerant of
both snake_case and camelCase so a reasonably nearby version still mostly works, but this module is
only TESTED against 2.0.0).

DESIGN, mirrored from ``adapters/acp_deep_runner.py`` (the closest existing precedent for talking a
generic external protocol from this repo):

  * The optional ``mcp`` package is imported LAZILY, inside the ONE seam function
    ``open_mcp_session`` -- an async context manager. Importing this module (or
    ``quest_ai_runner.adapters``) never requires the package to be installed.
  * The underlying SDK is async; every other adapter in this repo is sync. ``MCPClient`` owns a
    dedicated event loop on its own background thread (started lazily on first use) and drives all
    protocol calls through it, exactly like ``AcpDeepRunner`` does for its ACP session.
  * NEVER raises. Every failure -- a missing ``[mcp]`` extra, a dead child process, a
    handshake/protocol error, a call timeout -- comes back as a returned value: a bool from
    ``connect``/``close``, an empty list from a ``list_*`` call (with a logged warning), or one of
    the ``MCP*Result``/``MCPDiscovery`` dataclasses with ``ok=False``/an ``error`` field. Nothing
    here ever propagates an exception to a caller.
  * stdio transport env is an EXPLICIT ALLOWLIST, never the parent process's full environment (hard
    rule #1: no secret leaks into a spawned child by accident). This is stricter than
    ``AcpDeepRunner.build_env``, which starts from a copy of the parent env and strips a denylist of
    known-sensitive keys -- that approach is only as safe as the denylist is complete. Here the
    child gets NOTHING from the parent except what the caller explicitly named in
    ``MCPServerSpec.env_allowlist``, plus ``PATH`` (not a secret) so a bare command name can still
    be resolved.
  * http transport auth is INJECTED via a ``TokenProvider`` callable, the same pattern
    ``adapters/google_chat_adapter.py`` uses (``static_token_provider`` / a consumer's own minting
    function). The client never knows how a bearer token is minted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger("quest-ai-runner.mcp_client")

# A token provider returns a bearer access token string (or None when it cannot mint one -- the
# client then connects with no Authorization header, which a well-behaved server rejects cleanly).
TokenProvider = Callable[[], Optional[str]]

DEFAULT_CALL_TIMEOUT_SECONDS = 60.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
# Safety cap on cursor-following pagination (list_tools/list_resources): a buggy or hostile server
# that never returns a null cursor must not hang this client in an infinite fetch loop.
MAX_PAGES = 50


class MCPUnavailable(RuntimeError):
    """The ``mcp`` client package could not be used (missing, or the spec is malformed). Caught
    internally; a caller of any public ``MCPClient`` method sees a returned value with an honest
    error, never this exception."""


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------

@dataclass
class MCPServerSpec:
    """Connection + policy spec for ONE MCP server -- everything ``MCPClient`` (and, one layer up,
    ``MCPRetrievalAdapter``) need to reach and safely operate it. Consumer-supplied, nothing baked
    in (this is what a ``RunnerConfig.mcp_servers`` entry is).

    Exactly one transport is used, selected by ``transport``:

      * ``"stdio"`` -- spawn ``command`` (with ``args``) as a child process and speak MCP over its
        stdin/stdout. ``env_allowlist`` is the child's ENTIRE environment (an explicit {name:
        value} map the caller supplies), never the parent process's environment -- see the module
        docstring. ``cwd`` optionally sets the child's working directory.
      * ``"http"`` -- connect to ``url`` over streamable HTTP. ``token_provider``, if given, mints a
        bearer token injected as an ``Authorization`` header (same injection pattern as
        ``google_chat_adapter.TokenProvider``); ``None`` connects with no auth header.

    ``alias`` and ``allowed_tools`` are carried here too so a ``RunnerConfig.mcp_servers`` entry is
    self-contained for ``build_orchestrator``'s wiring, but ``MCPRetrievalAdapter`` takes its own
    explicit ``alias``/``allowed_tools`` constructor arguments as the authoritative values (letting
    a test or a consumer wrap one spec in adapters under different aliases/policies without cloning
    the spec). ``MCPClient`` itself only reads the connection fields; it ignores ``alias`` and
    ``allowed_tools``.
    """
    alias: str
    transport: str                                    # "stdio" | "http"
    # --- stdio ---
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env_allowlist: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    # --- http ---
    url: Optional[str] = None
    token_provider: Optional[TokenProvider] = None
    # --- shared policy (informational at the MCPClient layer; authoritative at the adapter layer) ---
    allowed_tools: List[str] = field(default_factory=list)
    timeout_s: float = DEFAULT_CALL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------------------------

@dataclass
class MCPDiscovery:
    """What ONE MCP server exposes, or why it could not be discovered. Never raised -- returned."""
    ok: bool
    tools: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    prompts: List[Dict[str, Any]] = field(default_factory=list)
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    instructions: Optional[str] = None
    error: Optional[str] = None


@dataclass
class MCPToolResult:
    """The outcome of ONE ``tools/call``. ``ok=False`` covers both a wire/timeout failure and a
    server-reported tool execution error (``isError=true``) -- either way nothing ran cleanly, so a
    caller checking only ``ok`` never has to separately handle a "successful call, failed tool"
    case."""
    ok: bool
    content: str = ""
    error: Optional[str] = None


@dataclass
class MCPResourceResult:
    """The outcome of ONE ``resources/read``."""
    ok: bool
    content: str = ""
    mime_type: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------------------------
# Tolerant field access -- the ``mcp`` SDK hands us pydantic models (snake_case attributes, some
# with camelCase wire aliases); a test double hands us plain dicts or ``SimpleNamespace``. Every
# read of a protocol payload goes through this, exactly like ``acp_deep_runner.field_of``.
# ---------------------------------------------------------------------------------------------

_MISSING = object()


def field_of(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute/key among ``names`` on ``obj``, else ``default``. Never raises."""
    if obj is None:
        return default
    for name in names:
        try:
            if isinstance(obj, dict):
                if name in obj:
                    return obj[name]
                continue
            value = getattr(obj, name, _MISSING)
            if value is not _MISSING:
                return value
        except Exception:  # noqa: BLE001 -- a hostile payload must never break translation
            continue
    return default


def _extract_text(content_blocks: Any) -> str:
    """Join a ``CallToolResult``/``ReadResourceResult`` content-block list into plain text.

    Text blocks are used verbatim; anything else (an image, an unrecognized block) becomes a short
    ``[kind]`` placeholder so nothing silently vanishes from the joined text. Never raises.
    """
    parts: List[str] = []
    for block in (content_blocks or []):
        try:
            text = field_of(block, "text")
            if text:
                parts.append(str(text))
                continue
            kind = field_of(block, "type") or "content"
            mime = field_of(block, "mime_type", "mimeType")
            parts.append(f"[{kind}{': ' + mime if mime else ''}]")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(parts).strip()


def _page_params(cursor: Optional[str]) -> Any:
    """Build the ``params`` object for a paginated ``list_tools``/``list_resources`` follow-up
    page. ``None`` for the first page (matches calling the method with no params at all).

    The real ``mcp`` SDK's ``ClientSession`` reads ``params.cursor`` directly (before pydantic gets
    a chance to coerce a plain dict), so a genuine ``mcp.types.PaginatedRequestParams`` instance is
    required there. That import only happens here, lazily, and only when a server actually returns
    a follow-up cursor (page 2+) -- which can only happen once ``open_mcp_session`` has already
    proven ``mcp`` is importable (a real connection would not exist otherwise). Offline tests never
    reach a real ``mcp`` install, so their fake session's cursor (if it scripts pagination) is read
    off this plain, duck-typed fallback instead -- both shapes expose the same ``.cursor``
    attribute, which is all either caller needs.
    """
    if cursor is None:
        return None
    try:
        from mcp import types as _types  # noqa: PLC0415 -- lazy; only reached post-connect
        return _types.PaginatedRequestParams(cursor=cursor)
    except ImportError:
        return SimpleNamespace(cursor=cursor)


def build_stdio_env(env_allowlist: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The stdio child's environment: an EXPLICIT allowlist, never the parent's full environment.

    ``env_allowlist`` becomes the entire child env, plus ``PATH`` (not a secret) so a bare command
    name can still be resolved on the child's search path if the caller didn't already include one.
    """
    env = dict(env_allowlist or {})
    if "PATH" not in env:
        parent_path = os.environ.get("PATH")
        if parent_path:
            env["PATH"] = parent_path
    return env


# ---------------------------------------------------------------------------------------------
# The live connection seam. Tests replace THIS symbol; nothing else in the module touches the SDK.
# ---------------------------------------------------------------------------------------------

@asynccontextmanager
async def open_mcp_session(spec: MCPServerSpec) -> AsyncIterator[Tuple[Any, Any]]:
    """Open a live MCP ``ClientSession`` per ``spec`` and yield ``(session, init_result)``.

    The ONE place the optional ``mcp`` package is imported and the one place a subprocess/socket is
    opened -- so an offline test replaces this single module-level name with its own async context
    manager and exercises the entire client with no protocol, no process, and no network (exactly
    ``acp_deep_runner.open_agent_connection``'s role for the ACP adapter).

    Raises on any failure (missing package, a malformed spec, a handshake error) -- ``MCPClient``,
    which drives this from its own background event loop, is the ONLY place those exceptions are
    caught and turned into a never-raise return value.
    """
    try:
        from mcp import ClientSession  # noqa: PLC0415 -- optional [mcp] extra, imported lazily
    except ImportError as e:  # pragma: no cover -- exercised via the error path, not the import
        raise MCPUnavailable(
            "the MCP client package is not installed. Install the optional extra: "
            "pip install 'quest-ai-runner[mcp]' (provides mcp==2.0.0)."
        ) from e

    transport = (spec.transport or "").strip().lower()
    if transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: PLC0415

        if not spec.command:
            raise ValueError("MCPServerSpec.transport='stdio' requires 'command' to be set")
        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args or []),
            env=build_stdio_env(spec.env_allowlist),
            cwd=spec.cwd,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=spec.timeout_s) as session:
                init_result = await session.initialize()
                yield session, init_result

    elif transport == "http":
        from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

        if not spec.url:
            raise ValueError("MCPServerSpec.transport='http' requires 'url' to be set")
        http_client = None
        if spec.token_provider is not None:
            token = spec.token_provider()
            if token:
                import httpx  # noqa: PLC0415 -- a transitive dep of ``mcp``, only needed for auth

                http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with streamable_http_client(spec.url, http_client=http_client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=spec.timeout_s) as session:
                init_result = await session.initialize()
                yield session, init_result

    else:
        raise ValueError(f"unknown MCPServerSpec.transport: {spec.transport!r} (want 'stdio' or 'http')")


# ---------------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------------

class MCPClient:
    """Raw MCP protocol client for ONE server. Owns a background event loop, never raises.

    Lazily connects on first use (``connect()`` is also callable explicitly / idempotently). Keeps
    the session open across calls -- ``close()`` tears it down. Every public method funnels its
    async work through ``_run``, the one place a timeout or a wire exception becomes a plain
    returned value instead of propagating.
    """

    def __init__(self, spec: MCPServerSpec):
        self._spec = spec
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cm: Any = None                # the open_mcp_session() async-cm instance, held open
        self._session: Any = None
        self._init_result: Any = None
        self._connect_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._session is not None

    # --- lifecycle -------------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the session (idempotent). Returns True once connected. Never raises."""
        with self._lock:
            if self._session is not None:
                return True
            if self._loop is None:
                self._start_loop()
            if self._loop is None:
                self._connect_error = "could not start the MCP client's event loop thread"
                return False
            fut = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
            try:
                ok = bool(fut.result(timeout=self._connect_timeout()))
            except FuturesTimeoutError:
                self._connect_error = (
                    f"MCP connect to {self._spec.alias!r} timed out after "
                    f"{self._connect_timeout():.0f}s"
                )
                ok = False
            except Exception as e:  # noqa: BLE001
                self._connect_error = f"{type(e).__name__}: {e}"
                ok = False
            if not ok:
                self._teardown_loop()
            return ok

    def close(self) -> None:
        """Close the session, if open. Never raises."""
        with self._lock:
            if self._cm is not None and self._loop is not None and not self._loop.is_closed():
                fut = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
                try:
                    fut.result(timeout=10.0)
                except Exception as e:  # noqa: BLE001 -- teardown never raises
                    _log.debug("mcp close for %s raised (ignored): %s", self._spec.alias, e)
            self._teardown_loop()
            self._session = None
            self._init_result = None
            self._cm = None

    def _connect_timeout(self) -> float:
        return max(5.0, float(self._spec.timeout_s or DEFAULT_CONNECT_TIMEOUT_SECONDS))

    def _start_loop(self) -> None:
        ready = threading.Event()

        def _run_forever() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run_forever, name=f"qar-mcp-{self._spec.alias}", daemon=True,
        )
        thread.start()
        if not ready.wait(timeout=5.0):
            self._thread = None
            self._loop = None
            return
        self._thread = thread

    def _teardown_loop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    async def _connect_async(self) -> bool:
        try:
            self._cm = open_mcp_session(self._spec)
            session, init_result = await self._cm.__aenter__()
            self._session = session
            self._init_result = init_result
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("mcp connect failed for %s: %s: %s", self._spec.alias, type(e).__name__, e)
            self._connect_error = f"{type(e).__name__}: {e}"
            self._cm = None
            return False

    async def _close_async(self) -> None:
        cm = self._cm
        self._cm = None
        if cm is not None:
            await cm.__aexit__(None, None, None)

    def _ensure_connected(self) -> bool:
        if self._session is not None:
            return True
        return self.connect()

    def _run(self, coro: Any, *, timeout: Optional[float] = None) -> Tuple[bool, Any, Optional[str]]:
        """Run ``coro`` on this client's loop with a timeout. Returns ``(ok, result, error)``.

        The ONE place every async failure (a raised exception, a hang past ``timeout``) funnels
        into a plain value. Never raises.
        """
        if self._loop is None or self._loop.is_closed():
            return False, None, f"not connected to {self._spec.alias!r}"
        effective_timeout = timeout if timeout is not None else (self._spec.timeout_s or DEFAULT_CALL_TIMEOUT_SECONDS)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return True, fut.result(timeout=effective_timeout), None
        except FuturesTimeoutError:
            fut.cancel()
            return False, None, (
                f"MCP call to {self._spec.alias!r} timed out after {effective_timeout:.0f}s"
            )
        except Exception as e:  # noqa: BLE001
            return False, None, f"{type(e).__name__}: {e}"

    # --- discovery ---------------------------------------------------------------------------

    def discover(self) -> MCPDiscovery:
        """Everything this server exposes: tools, resources, prompts. Never raises."""
        if not self._ensure_connected():
            return MCPDiscovery(ok=False, error=self._connect_error or f"could not connect to {self._spec.alias!r}")
        tools = self.list_tools()
        resources = self.list_resources()
        prompts = self._list_prompts()
        server_info = field_of(self._init_result, "server_info", "serverInfo")
        return MCPDiscovery(
            ok=True,
            tools=tools,
            resources=resources,
            prompts=prompts,
            server_name=field_of(server_info, "name"),
            server_version=field_of(server_info, "version"),
            instructions=field_of(self._init_result, "instructions"),
        )

    def list_tools(self) -> List[Dict[str, Any]]:
        """``[{name, description, input_schema}, ...]``. Empty list + logged warning on failure.
        Follows pagination (bounded by ``MAX_PAGES``) so the full catalog is returned. Never raises.
        """
        if not self._ensure_connected():
            _log.warning("mcp list_tools(%s): not connected (%s)", self._spec.alias, self._connect_error)
            return []
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            ok, result, error = self._run(self._session.list_tools(params=_page_params(cursor)))
            if not ok:
                _log.warning("mcp list_tools(%s) failed: %s", self._spec.alias, error)
                break
            for t in (field_of(result, "tools", default=[]) or []):
                name = field_of(t, "name")
                if not name:
                    continue
                out.append({
                    "name": name,
                    "description": field_of(t, "description") or "",
                    "input_schema": field_of(t, "input_schema", "inputSchema") or {},
                })
            cursor = field_of(result, "next_cursor", "nextCursor")
            if not cursor:
                break
        return out

    def list_resources(self) -> List[Dict[str, Any]]:
        """``[{uri, name, description, mime_type}, ...]``. Empty list + logged warning on failure.
        Follows pagination (bounded by ``MAX_PAGES``). Never raises."""
        if not self._ensure_connected():
            _log.warning("mcp list_resources(%s): not connected (%s)", self._spec.alias, self._connect_error)
            return []
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            ok, result, error = self._run(self._session.list_resources(params=_page_params(cursor)))
            if not ok:
                _log.warning("mcp list_resources(%s) failed: %s", self._spec.alias, error)
                break
            for r in (field_of(result, "resources", default=[]) or []):
                uri = field_of(r, "uri")
                if not uri:
                    continue
                out.append({
                    "uri": uri,
                    "name": field_of(r, "name") or "",
                    "description": field_of(r, "description") or "",
                    "mime_type": field_of(r, "mime_type", "mimeType"),
                })
            cursor = field_of(result, "next_cursor", "nextCursor")
            if not cursor:
                break
        return out

    def _list_prompts(self) -> List[Dict[str, Any]]:
        """Best-effort; a server with no prompts capability degrades to []. Never raises."""
        ok, result, error = self._run(self._session.list_prompts())
        if not ok:
            _log.debug("mcp list_prompts(%s) unavailable: %s", self._spec.alias, error)
            return []
        out = []
        for p in (field_of(result, "prompts", default=[]) or []):
            name = field_of(p, "name")
            if not name:
                continue
            out.append({"name": name, "description": field_of(p, "description") or ""})
        return out

    # --- operations ---------------------------------------------------------------------------

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None, *,
                  timeout: Optional[float] = None) -> MCPToolResult:
        """Call one tool. ``ok=False`` covers a wire failure AND a server-reported tool error.
        Never raises."""
        if not self._ensure_connected():
            return MCPToolResult(ok=False, error=self._connect_error or f"could not connect to {self._spec.alias!r}")
        ok, result, error = self._run(self._session.call_tool(name, args or {}), timeout=timeout)
        if not ok:
            return MCPToolResult(ok=False, error=error)
        content = _extract_text(field_of(result, "content", default=[]))
        is_error = bool(field_of(result, "is_error", "isError", default=False))
        if is_error:
            return MCPToolResult(ok=False, content=content, error=content or f"tool {name!r} reported an error")
        return MCPToolResult(ok=True, content=content)

    def read_resource(self, uri: str, *, timeout: Optional[float] = None) -> MCPResourceResult:
        """Read one resource by URI. Never raises."""
        if not self._ensure_connected():
            return MCPResourceResult(ok=False, error=self._connect_error or f"could not connect to {self._spec.alias!r}")
        ok, result, error = self._run(self._session.read_resource(uri), timeout=timeout)
        if not ok:
            return MCPResourceResult(ok=False, error=error)
        contents = field_of(result, "contents", default=[]) or []
        if not contents:
            return MCPResourceResult(ok=False, error=f"resource {uri!r} returned no content")
        first = contents[0]
        mime = field_of(first, "mime_type", "mimeType")
        text = field_of(first, "text")
        if text is not None:
            return MCPResourceResult(ok=True, content=str(text), mime_type=mime)
        blob = field_of(first, "blob")
        if blob is not None:
            return MCPResourceResult(
                ok=True, content=f"[binary resource, {len(str(blob))} base64 chars]", mime_type=mime,
            )
        return MCPResourceResult(ok=False, error=f"resource {uri!r} returned an unrecognized content shape")
