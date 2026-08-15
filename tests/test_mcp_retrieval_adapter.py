"""Offline tests for ``MCPRetrievalAdapter`` (``adapters/mcp_retrieval_adapter.py``).

Fully offline: no real ``mcp`` package, no subprocess, no network. ``MCPRetrievalAdapter`` talks to
an ``MCPClient`` through a small, entirely synchronous surface (``list_tools`` / ``list_resources``
/ ``call_tool`` / ``read_resource``), so these tests inject a ``FakeMCPClient`` double (the adapter
accepts a ``client=`` override) that scripts that surface directly -- the retrieval-adapter-level
analogue of ``test_mcp_client.py`` monkeypatching ``open_mcp_session``. ``test_mcp_client.py``
separately proves the real ``MCPClient`` <-> ``open_mcp_session`` seam; this file proves the mapping
from THAT surface onto QAR's ``RetrievalAdapter`` contract.

What is pinned here:
  * a parametrized table of >= 4 server shapes (tools-only, resources-only, both, paginated
    resources) all satisfied by the SAME adapter class and the SAME assertions -- the genericity
    proof;
  * degradation: the [mcp] extra missing, a mid-session connection death, a tool-call error, a
    timeout, an un-allowlisted tool call (refused BEFORE MCPClient.call_tool is ever reached --
    verified with a spy), and a missing required schema arg (the error surfaces the tool's own
    schema);
  * structural RetrievalAdapter conformance;
  * hard rule #1: no real hub/vendor/company name is hardcoded in the adapter's own source;
  * a CompositeRetrievalAdapter collision test: two aliased adapters exposing an identically-named
    tool both stay reachable and distinguishable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.adapters.mcp_client import MCPResourceResult, MCPServerSpec, MCPToolResult
from quest_ai_runner.adapters.mcp_retrieval_adapter import MCPRetrievalAdapter
from quest_ai_runner.core.adapters import RetrievalAdapter


# ---------------------------------------------------------------------------------------------
# FakeMCPClient -- scripts MCPClient's public (synchronous) surface directly. No asyncio, no SDK.
# ---------------------------------------------------------------------------------------------

class FakeMCPClient:
    def __init__(
        self,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        resources: Optional[List[Dict[str, Any]]] = None,
        call_tool_results: Optional[Dict[str, MCPToolResult]] = None,
        read_resource_results: Optional[Dict[str, MCPResourceResult]] = None,
        default_tool_result: Optional[MCPToolResult] = None,
    ):
        self.tools = tools or []
        self.resources = resources or []
        self.call_tool_results = call_tool_results or {}
        self.read_resource_results = read_resource_results or {}
        self.default_tool_result = default_tool_result
        self.call_tool_calls: List[tuple] = []
        self.read_resource_calls: List[str] = []

    def list_tools(self) -> List[Dict[str, Any]]:
        return list(self.tools)

    def list_resources(self) -> List[Dict[str, Any]]:
        return list(self.resources)

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None, *, timeout: Optional[float] = None) -> MCPToolResult:
        self.call_tool_calls.append((name, args))
        if name in self.call_tool_results:
            return self.call_tool_results[name]
        if self.default_tool_result is not None:
            return self.default_tool_result
        return MCPToolResult(ok=True, content=f"result of {name}")

    def read_resource(self, uri: str, *, timeout: Optional[float] = None) -> MCPResourceResult:
        self.read_resource_calls.append(uri)
        if uri in self.read_resource_results:
            return self.read_resource_results[uri]
        return MCPResourceResult(ok=True, content=f"content of {uri}", mime_type="text/plain")


def tool(name: str, description: str = "", schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"name": name, "description": description, "input_schema": schema or {}}


def resource(uri: str, name: str = "", description: str = "", mime_type: str = "text/plain") -> Dict[str, Any]:
    return {"uri": uri, "name": name, "description": description, "mime_type": mime_type}


def spec(**overrides: Any) -> MCPServerSpec:
    base: Dict[str, Any] = dict(alias="srv", transport="stdio", command="fake", timeout_s=5.0)
    base.update(overrides)
    return MCPServerSpec(**base)


def make_adapter(fake: FakeMCPClient, *, alias: str = "srv", allowed_tools: Optional[List[str]] = None) -> MCPRetrievalAdapter:
    return MCPRetrievalAdapter(
        alias=alias,
        spec=spec(alias=alias, allowed_tools=allowed_tools or [t["name"] for t in fake.tools]),
        allowed_tools=allowed_tools if allowed_tools is not None else [t["name"] for t in fake.tools],
        client=fake,
    )


# ---------------------------------------------------------------------------------------------
# 1. Genericity: 4+ server fixtures, same adapter class, same assertions.
# ---------------------------------------------------------------------------------------------

def fixture_tools_only() -> FakeMCPClient:
    return FakeMCPClient(tools=[
        tool("search", "Search things", {"type": "object", "properties": {"q": {"type": "string"}}}),
        tool("get", "Get a thing"),
    ])


def fixture_resources_only() -> FakeMCPClient:
    return FakeMCPClient(resources=[
        resource("repo://readme", "README", "the readme"),
        resource("repo://config", "Config", "the config"),
    ])


def fixture_both() -> FakeMCPClient:
    return FakeMCPClient(
        tools=[tool("search", "Search things")],
        resources=[resource("repo://readme", "README", "the readme")],
    )


def fixture_paginated_resources() -> FakeMCPClient:
    # MCPClient itself resolves pagination internally (proven in test_mcp_client.py); by the time
    # MCPRetrievalAdapter sees list_resources(), it is already a flat, fully-materialized list --
    # this fixture is a LARGE one (as a multi-page fetch would produce) to prove the adapter handles
    # that shape identically to a small one.
    return FakeMCPClient(
        tools=[tool("search", "Search things")],
        resources=[resource(f"repo://file{i}", f"file{i}", f"file number {i}") for i in range(25)],
    )


FIXTURES = {
    "tools_only": fixture_tools_only,
    "resources_only": fixture_resources_only,
    "both": fixture_both,
    "paginated_resources": fixture_paginated_resources,
}


@pytest.mark.parametrize("fixture_name", list(FIXTURES.keys()))
def test_adapter_is_generic_across_server_shapes(fixture_name):
    fake = FIXTURES[fixture_name]()
    adapter = make_adapter(fake)

    sources_obs = adapter.list_sources()
    assert sources_obs.kind != "error"
    if fake.resources:
        for r in fake.resources:
            assert f"srv:{r['uri']}" in sources_obs.text
    else:
        assert "no" in sources_obs.text.lower() or "No" in sources_obs.text

    ops_obs = adapter.list_operations()
    assert ops_obs.kind != "error"
    if fake.tools:
        for t in fake.tools:
            assert f"srv:{t['name']}" in ops_obs.text
    else:
        assert "no" in ops_obs.text.lower() or "No" in ops_obs.text

    if fake.tools:
        first_tool = fake.tools[0]["name"]
        q = adapter.query({"tool": f"srv:{first_tool}", "args": {}})
        assert q.kind == "query"
        assert q.text

        desc = adapter.describe_operation(f"srv:{first_tool}")
        assert desc.kind != "error"
        assert first_tool in desc.text

    if fake.resources:
        first_uri = fake.resources[0]["uri"]
        read = adapter.read_section(f"srv:{first_uri}")
        assert read.kind == "read"
        assert read.text

        desc = adapter.describe_source(f"srv:{first_uri}")
        assert desc.kind != "error"


def test_paginated_fixture_surfaces_all_25_resources():
    fake = fixture_paginated_resources()
    adapter = make_adapter(fake)
    obs = adapter.list_sources()
    for i in range(25):
        assert f"srv:repo://file{i}" in obs.text


# ---------------------------------------------------------------------------------------------
# 2. Degradation cases
# ---------------------------------------------------------------------------------------------

def test_extra_not_installed_degrades_gracefully():
    """Simulates the [mcp] extra missing: MCPClient (or here, its stand-in) reports nothing
    available. The adapter must still answer honestly, not crash."""
    fake = FakeMCPClient(tools=[], resources=[])  # what a never-connected client reports
    adapter = make_adapter(fake, allowed_tools=[])
    sources = adapter.list_sources()
    assert sources.kind != "error"
    assert "no" in sources.text.lower()
    ops = adapter.list_operations()
    assert ops.kind != "error"
    assert "no" in ops.text.lower()
    q = adapter.query({"tool": "anything", "args": {}})
    assert q.kind == "error"


def test_connection_dies_mid_session_surfaces_as_error_observation():
    fake = FakeMCPClient(tools=[tool("search")], resources=[resource("repo://readme")])
    adapter = make_adapter(fake)
    # Simulate the process dying after discovery succeeded but before the call landed.
    fake.call_tool_results["search"] = MCPToolResult(ok=False, error="RuntimeError: connection reset by peer")
    result = adapter.query({"tool": "srv:search", "args": {}})
    assert result.kind == "error"
    assert "connection reset" in result.error


def test_tool_call_error_is_surfaced():
    fake = FakeMCPClient(tools=[tool("search")])
    fake.call_tool_results["search"] = MCPToolResult(ok=False, error="tool 'search' reported an error", content="bad query")
    adapter = make_adapter(fake)
    result = adapter.query({"tool": "srv:search", "args": {"q": "??"}})
    assert result.kind == "error"
    assert "reported an error" in result.error


def test_timeout_is_surfaced_as_error():
    fake = FakeMCPClient(tools=[tool("search")])
    fake.call_tool_results["search"] = MCPToolResult(ok=False, error="MCP call to 'srv' timed out after 5s")
    adapter = make_adapter(fake)
    result = adapter.query({"tool": "srv:search", "args": {}})
    assert result.kind == "error"
    assert "timed out" in result.error


def test_non_allowlisted_tool_is_refused_and_never_reaches_call_tool():
    fake = FakeMCPClient(tools=[tool("search"), tool("delete_everything")])
    adapter = make_adapter(fake, allowed_tools=["search"])  # delete_everything NOT allowed
    result = adapter.query({"tool": "srv:delete_everything", "args": {}})
    assert result.kind == "error"
    assert "not in allowed_tools" in result.error
    assert fake.call_tool_calls == []  # the spy: call_tool was NEVER invoked


def test_allowlisted_tool_call_succeeds():
    fake = FakeMCPClient(tools=[tool("search")])
    adapter = make_adapter(fake, allowed_tools=["search"])
    result = adapter.query({"tool": "srv:search", "args": {"q": "x"}})
    assert result.kind == "query"
    assert fake.call_tool_calls == [("search", {"q": "x"})]


def test_missing_required_arg_surfaces_the_servers_own_schema():
    schema = {"type": "object", "required": ["repo"], "properties": {"repo": {"type": "string"}}}
    fake = FakeMCPClient(tools=[tool("search", "Search", schema)])
    fake.call_tool_results["search"] = MCPToolResult(ok=False, error="missing required argument: repo")
    adapter = make_adapter(fake)
    result = adapter.query({"tool": "srv:search", "args": {}})
    assert result.kind == "error"
    assert "missing required argument" in result.error
    # the schema is surfaced so the orchestrator could replan with the right args
    assert '"repo"' in result.error
    assert "required" in result.error


def test_grep_is_honestly_unsupported():
    fake = FakeMCPClient()
    adapter = make_adapter(fake)
    result = adapter.grep("anything")
    assert result.kind == "error"
    assert "grep" in result.error.lower()
    assert "not supported" in result.error.lower()


def test_bare_tool_name_also_accepted_by_query():
    """A caller that already knows the bare (non-aliased) tool name may pass it directly."""
    fake = FakeMCPClient(tools=[tool("search")])
    adapter = make_adapter(fake, allowed_tools=["search"])
    result = adapter.query({"tool": "search", "args": {}})
    assert result.kind == "query"


def test_foreign_alias_name_is_refused_by_describe_and_read():
    fake = FakeMCPClient(tools=[tool("search")], resources=[resource("repo://readme")])
    adapter = make_adapter(fake, alias="srv")
    assert adapter.describe_operation("other:search").kind == "error"
    assert adapter.describe_source("other:repo://readme").kind == "error"
    assert adapter.read_section("other:repo://readme").kind == "error"


# ---------------------------------------------------------------------------------------------
# 3. Structural RetrievalAdapter conformance
# ---------------------------------------------------------------------------------------------

def test_adapter_structurally_satisfies_retrieval_adapter_protocol():
    adapter = make_adapter(FakeMCPClient(tools=[tool("search")]))
    assert isinstance(adapter, RetrievalAdapter)


# ---------------------------------------------------------------------------------------------
# 4. Hard rule #1: no real hub/vendor/company name hardcoded in this adapter's own source.
# ---------------------------------------------------------------------------------------------

BANNED_REAL_NAMES = (
    "github", "gitlab", "bitbucket", "jira", "confluence", "slack", "notion", "salesforce",
    "zendesk", "asana", "trello", "linear.app", "hubspot", "anthropic", "spiritualdata",
    "quest-backend", "quest-frontend",
)


def test_no_real_vendor_name_hardcoded_in_adapter_source():
    src_path = Path(__file__).resolve().parents[1] / "quest_ai_runner" / "adapters" / "mcp_retrieval_adapter.py"
    text = src_path.read_text(encoding="utf-8").lower()
    hits = [name for name in BANNED_REAL_NAMES if name in text]
    assert not hits, f"real vendor/org name(s) hardcoded in mcp_retrieval_adapter.py: {hits}"


# ---------------------------------------------------------------------------------------------
# 5. CompositeRetrievalAdapter collision test
# ---------------------------------------------------------------------------------------------

def test_two_aliased_adapters_with_same_tool_name_both_stay_reachable():
    """Two different MCP servers exposing an identically-named tool ('search'), wrapped in
    MCPRetrievalAdapters under DIFFERENT aliases, must both remain independently reachable through
    CompositeRetrievalAdapter's per-adapter delegation (describe_operation / query), not collapse
    into one."""
    fake_a = FakeMCPClient(tools=[tool("search", "Search server A")])
    fake_a.call_tool_results["search"] = MCPToolResult(ok=True, content="result from A")
    adapter_a = make_adapter(fake_a, alias="alpha", allowed_tools=["search"])

    fake_b = FakeMCPClient(tools=[tool("search", "Search server B")])
    fake_b.call_tool_results["search"] = MCPToolResult(ok=True, content="result from B")
    adapter_b = make_adapter(fake_b, alias="beta", allowed_tools=["search"])

    composite = CompositeRetrievalAdapter([adapter_a, adapter_b])

    desc_a = composite.describe_operation("alpha:search")
    desc_b = composite.describe_operation("beta:search")
    assert desc_a.kind != "error" and desc_b.kind != "error"
    assert "Search server A" in desc_a.text
    assert "Search server B" in desc_b.text
    assert desc_a.text != desc_b.text

    q_a = composite.query({"tool": "alpha:search", "args": {}})
    q_b = composite.query({"tool": "beta:search", "args": {}})
    # Both succeed via SOME adapter in the composite; each call only ever reached its OWN server.
    assert q_a.kind == "query" and "result from A" in q_a.text
    assert q_b.kind == "query" and "result from B" in q_b.text
    assert fake_a.call_tool_calls == [("search", {})]
    assert fake_b.call_tool_calls == [("search", {})]
