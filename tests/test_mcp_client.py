"""Offline tests for the raw MCP protocol client (``adapters/mcp_client.py``).

Fully offline by construction: the optional ``mcp`` package is never imported, no subprocess is
spawned, no socket is touched. The client concentrates all of that behind ONE module-level seam --
``open_mcp_session`` -- so these tests replace that symbol with a scripted fake session and drive
the real client end to end, exactly like ``tests/test_acp_deep_runner.py`` does for the ACP runner.

What is pinned here:
  * connect/discover/list_tools/call_tool/list_resources/read_resource each have a success case and
    a raising-fake case (the raise must become a returned value, never propagate);
  * pagination (list_tools/list_resources follow a next_cursor across pages);
  * a call that exceeds its timeout comes back as ok=False with a timeout message, not a hang;
  * close() is idempotent and never raises, even if the underlying session's teardown raises.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters import mcp_client as mc


def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


TOOL_A = ns(name="search", description="Search things", input_schema={"type": "object", "properties": {"q": {"type": "string"}}})
TOOL_B = ns(name="get", description="Get a thing", input_schema={"type": "object"})
RESOURCE_A = ns(uri="repo://readme", name="README", description="The readme", mime_type="text/plain")
RESOURCE_B = ns(uri="repo://config", name="Config", description="Config file", mime_type="text/plain")


class FakeSession:
    """A stand-in for ``mcp.ClientSession`` driven by scripted, per-call behavior."""

    def __init__(
        self,
        *,
        tools: Optional[List[Any]] = None,
        resources: Optional[List[Any]] = None,
        prompts: Optional[List[Any]] = None,
        tool_pages: Optional[List[List[Any]]] = None,
        resource_pages: Optional[List[List[Any]]] = None,
        call_tool_result: Any = None,
        call_tool_error: Optional[Exception] = None,
        call_tool_delay: float = 0.0,
        read_resource_result: Any = None,
        read_resource_error: Optional[Exception] = None,
        list_tools_error: Optional[Exception] = None,
        list_resources_error: Optional[Exception] = None,
        server_name: str = "fake-server",
        server_version: str = "1.0",
        instructions: str = "a fake mcp server",
    ):
        self.tools = tools or []
        self.resources = resources or []
        self.prompts = prompts or []
        # tool_pages / resource_pages: an explicit list of PAGES (each a list of items) for
        # exercising cursor-follow pagination; when set, overrides the flat `tools`/`resources`.
        self.tool_pages = tool_pages
        self.resource_pages = resource_pages
        self.call_tool_result = call_tool_result
        self.call_tool_error = call_tool_error
        self.call_tool_delay = call_tool_delay
        self.read_resource_result = read_resource_result
        self.read_resource_error = read_resource_error
        self.list_tools_error = list_tools_error
        self.list_resources_error = list_resources_error
        self.server_name = server_name
        self.server_version = server_version
        self.instructions = instructions
        self.calls: List[tuple] = []
        self.closed = False

    async def list_tools(self, *, params: Any = None):
        self.calls.append(("list_tools", params))
        if self.list_tools_error is not None:
            raise self.list_tools_error
        if self.tool_pages is not None:
            idx = 0 if params is None else int(getattr(params, "cursor", "0") or 0)
            page = self.tool_pages[idx] if idx < len(self.tool_pages) else []
            next_idx = idx + 1
            next_cursor = str(next_idx) if next_idx < len(self.tool_pages) else None
            return ns(tools=page, next_cursor=next_cursor)
        return ns(tools=self.tools, next_cursor=None)

    async def list_resources(self, *, params: Any = None):
        self.calls.append(("list_resources", params))
        if self.list_resources_error is not None:
            raise self.list_resources_error
        if self.resource_pages is not None:
            idx = 0 if params is None else int(getattr(params, "cursor", "0") or 0)
            page = self.resource_pages[idx] if idx < len(self.resource_pages) else []
            next_idx = idx + 1
            next_cursor = str(next_idx) if next_idx < len(self.resource_pages) else None
            return ns(resources=page, next_cursor=next_cursor)
        return ns(resources=self.resources, next_cursor=None)

    async def list_prompts(self):
        self.calls.append(("list_prompts", None))
        return ns(prompts=self.prompts)

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None):
        self.calls.append(("call_tool", name, arguments))
        if self.call_tool_delay:
            await asyncio.sleep(self.call_tool_delay)
        if self.call_tool_error is not None:
            raise self.call_tool_error
        if self.call_tool_result is not None:
            return self.call_tool_result
        return ns(content=[ns(type="text", text=f"called {name}")], is_error=False)

    async def read_resource(self, uri: str):
        self.calls.append(("read_resource", uri))
        if self.read_resource_error is not None:
            raise self.read_resource_error
        if self.read_resource_result is not None:
            return self.read_resource_result
        return ns(contents=[ns(uri=uri, text=f"content of {uri}", mime_type="text/plain")])


def fake_init_result(session: FakeSession) -> Any:
    return ns(
        server_info=ns(name=session.server_name, version=session.server_version),
        instructions=session.instructions,
    )


def install_fake_session(monkeypatch, session: FakeSession, *, connect_error: Optional[Exception] = None):
    """Replace the ONE seam that touches the SDK/subprocess/socket with a scripted fake."""

    @asynccontextmanager
    async def fake_open(spec: mc.MCPServerSpec):
        if connect_error is not None:
            raise connect_error
        yield session, fake_init_result(session)

    monkeypatch.setattr(mc, "open_mcp_session", fake_open)
    return session


def spec(**overrides: Any) -> mc.MCPServerSpec:
    base: Dict[str, Any] = dict(alias="fake", transport="stdio", command="fake-mcp-server", timeout_s=5.0)
    base.update(overrides)
    return mc.MCPServerSpec(**base)


@pytest.fixture
def _close_clients():
    """Tests append every ``MCPClient`` they create here; closed at teardown so no background
    thread/loop leaks between tests."""
    created: List[mc.MCPClient] = []
    yield created
    for c in created:
        c.close()


# --- connect / close -----------------------------------------------------------------------

def test_connect_success(monkeypatch, _close_clients):
    session = FakeSession()
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    assert client.connect() is True
    assert client.connect() is True  # idempotent
    client.close()
    assert client.connected is False


def test_connect_failure_never_raises(monkeypatch, _close_clients):
    """A raising fake connection must become a returned False, never propagate."""
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    install_fake_session(monkeypatch, FakeSession(), connect_error=RuntimeError("boom"))
    assert client.connect() is False
    assert client.connected is False


def test_mcp_unavailable_surfaces_as_connect_failure(monkeypatch, _close_clients):
    """Simulates the [mcp] extra not being installed: the seam raises MCPUnavailable."""
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    install_fake_session(
        monkeypatch, FakeSession(),
        connect_error=mc.MCPUnavailable("the MCP client package is not installed"),
    )
    assert client.connect() is False
    assert "not installed" in (client._connect_error or "")


def test_close_is_idempotent_and_never_raises(monkeypatch, _close_clients):
    class RaisingCloseSession(FakeSession):
        pass

    session = RaisingCloseSession()
    client = mc.MCPClient(spec())
    _close_clients.append(client)

    @asynccontextmanager
    async def fake_open(s):
        try:
            yield session, fake_init_result(session)
        finally:
            raise RuntimeError("teardown exploded")

    monkeypatch.setattr(mc, "open_mcp_session", fake_open)
    assert client.connect() is True
    client.close()  # must not raise despite the teardown exception
    client.close()  # idempotent
    assert client.connected is False


# --- discover -------------------------------------------------------------------------------

def test_discover_success(monkeypatch, _close_clients):
    session = FakeSession(tools=[TOOL_A, TOOL_B], resources=[RESOURCE_A], prompts=[ns(name="p1", description="d1")])
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)

    disc = client.discover()
    assert disc.ok is True
    assert disc.error is None
    assert {t["name"] for t in disc.tools} == {"search", "get"}
    assert disc.resources[0]["uri"] == "repo://readme"
    assert disc.prompts[0]["name"] == "p1"
    assert disc.server_name == "fake-server"
    assert disc.server_version == "1.0"
    assert disc.instructions == "a fake mcp server"


def test_discover_when_connect_fails_never_raises(monkeypatch, _close_clients):
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    install_fake_session(monkeypatch, FakeSession(), connect_error=RuntimeError("no route to host"))
    disc = client.discover()
    assert disc.ok is False
    assert disc.error
    assert disc.tools == [] and disc.resources == []


# --- list_tools -------------------------------------------------------------------------------

def test_list_tools_success(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(tools=[TOOL_A, TOOL_B]))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    tools = client.list_tools()
    assert len(tools) == 2
    assert tools[0]["name"] == "search"
    assert tools[0]["description"] == "Search things"
    assert tools[0]["input_schema"]["type"] == "object"


def test_list_tools_raising_fake_returns_empty_list_never_raises(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(list_tools_error=RuntimeError("wire error")))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    assert client.list_tools() == []


def test_list_tools_paginates_across_pages(monkeypatch, _close_clients):
    session = FakeSession(tool_pages=[[TOOL_A], [TOOL_B], [ns(name="third", description="", input_schema={})]])
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    tools = client.list_tools()
    assert [t["name"] for t in tools] == ["search", "get", "third"]
    # three list_tools calls: page 0 (params=None), page 1 (cursor "1"), page 2 (cursor "2")
    assert len([c for c in session.calls if c[0] == "list_tools"]) == 3


# --- list_resources ---------------------------------------------------------------------------

def test_list_resources_success(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(resources=[RESOURCE_A, RESOURCE_B]))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    resources = client.list_resources()
    assert len(resources) == 2
    assert resources[0]["uri"] == "repo://readme"
    assert resources[0]["mime_type"] == "text/plain"


def test_list_resources_raising_fake_returns_empty_list_never_raises(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(list_resources_error=RuntimeError("wire error")))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    assert client.list_resources() == []


def test_list_resources_paginates_across_pages(monkeypatch, _close_clients):
    session = FakeSession(resource_pages=[[RESOURCE_A], [RESOURCE_B]])
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    resources = client.list_resources()
    assert [r["uri"] for r in resources] == ["repo://readme", "repo://config"]


# --- call_tool --------------------------------------------------------------------------------

def test_call_tool_success(monkeypatch, _close_clients):
    session = FakeSession(call_tool_result=ns(content=[ns(type="text", text="42")], is_error=False))
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.call_tool("search", {"q": "life"})
    assert result.ok is True
    assert result.content == "42"
    assert result.error is None
    assert session.calls[0] == ("call_tool", "search", {"q": "life"})


def test_call_tool_server_reported_error_is_not_ok(monkeypatch, _close_clients):
    session = FakeSession(call_tool_result=ns(content=[ns(type="text", text="missing arg: q")], is_error=True))
    install_fake_session(monkeypatch, session)
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.call_tool("search", {})
    assert result.ok is False
    assert "missing arg" in result.error


def test_call_tool_raising_fake_returns_error_result_never_raises(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(call_tool_error=RuntimeError("connection reset")))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.call_tool("search", {})
    assert result.ok is False
    assert "connection reset" in result.error


def test_call_tool_timeout_returns_error_never_hangs(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(call_tool_delay=2.0))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    start = time.time()
    result = client.call_tool("search", {}, timeout=0.2)
    elapsed = time.time() - start
    assert result.ok is False
    assert "timed out" in result.error
    assert elapsed < 1.5  # returned promptly, not after the full 2s delay


# --- read_resource ----------------------------------------------------------------------------

def test_read_resource_success(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession())
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.read_resource("repo://readme")
    assert result.ok is True
    assert result.content == "content of repo://readme"
    assert result.mime_type == "text/plain"


def test_read_resource_raising_fake_returns_error_result_never_raises(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(read_resource_error=RuntimeError("404")))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.read_resource("repo://missing")
    assert result.ok is False
    assert "404" in result.error


def test_read_resource_empty_contents_is_an_honest_error(monkeypatch, _close_clients):
    install_fake_session(monkeypatch, FakeSession(read_resource_result=ns(contents=[])))
    client = mc.MCPClient(spec())
    _close_clients.append(client)
    result = client.read_resource("repo://empty")
    assert result.ok is False
    assert "no content" in result.error


# --- env allowlist --------------------------------------------------------------------------

def test_build_stdio_env_is_an_explicit_allowlist_not_the_parent_env(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-never-appear")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = mc.build_stdio_env({"GITHUB_TOKEN": "abc123"})
    assert env["GITHUB_TOKEN"] == "abc123"
    assert "SUPER_SECRET_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"  # PATH is admitted (not a secret), everything else is not


def test_build_stdio_env_with_no_allowlist_is_just_path(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-never-appear")
    env = mc.build_stdio_env(None)
    assert "SUPER_SECRET_TOKEN" not in env
    assert set(env.keys()) <= {"PATH"}
