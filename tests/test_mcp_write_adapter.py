"""Offline tests for ``MCPWriteAdapter`` (``adapters/mcp_write_adapter.py``).

Fully offline: no real ``mcp`` package, no subprocess, no network. Mirrors
``test_mcp_retrieval_adapter.py``'s rigor and structure for the WRITE side: a ``FakeMCPClient``
double (the adapter accepts a ``client=`` override) scripts ``MCPClient``'s synchronous surface
(``list_tools`` / ``call_tool``) directly.

What is pinned here:
  * a tool not on ``writable_tools`` is refused with ZERO network calls (spy-verified on
    ``call_tool_calls``);
  * a successful write returns ``ok=True`` with the executed tool/args recorded in ``detail``;
  * a failing/erroring tool call returns ``ok=False``, never raises, and still records the
    attempted tool/args in ``detail``;
  * ``writable_tools`` is a SEPARATE allowlist from a read-side ``allowed_tools`` -- being
    read-allowlisted grants no write access;
  * ``list_writable_operations`` discovery: filters to the allowlist, aliases names, degrades to
    ``[]`` on a discovery failure;
  * structural ``OperationWriter`` conformance;
  * hard rule #1: no real hub/vendor/company name is hardcoded in the adapter's own source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.mcp_client import MCPServerSpec, MCPToolResult
from quest_ai_runner.adapters.mcp_write_adapter import MCPWriteAdapter
from quest_ai_runner.core.adapters import OperationWriter, WriteResult


# ---------------------------------------------------------------------------------------------
# FakeMCPClient -- scripts MCPClient's public (synchronous) surface directly. No asyncio, no SDK.
# ---------------------------------------------------------------------------------------------

class FakeMCPClient:
    def __init__(
        self,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        call_tool_results: Optional[Dict[str, MCPToolResult]] = None,
        default_tool_result: Optional[MCPToolResult] = None,
    ):
        self.tools = tools or []
        self.call_tool_results = call_tool_results or {}
        self.default_tool_result = default_tool_result
        self.call_tool_calls: List[tuple] = []
        self.list_tools_calls = 0

    def list_tools(self) -> List[Dict[str, Any]]:
        self.list_tools_calls += 1
        return list(self.tools)

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None, *,
                  timeout: Optional[float] = None) -> MCPToolResult:
        self.call_tool_calls.append((name, args))
        if name in self.call_tool_results:
            return self.call_tool_results[name]
        if self.default_tool_result is not None:
            return self.default_tool_result
        return MCPToolResult(ok=True, content=f"result of {name}")


class ExplodingMCPClient(FakeMCPClient):
    """A discovery call that raises -- proves list_writable_operations degrades, never raises."""

    def list_tools(self) -> List[Dict[str, Any]]:
        raise RuntimeError("connection dead")


def tool(name: str, description: str = "", schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"name": name, "description": description, "input_schema": schema or {}}


def spec(**overrides: Any) -> MCPServerSpec:
    base: Dict[str, Any] = dict(alias="srv", transport="stdio", command="fake", timeout_s=5.0)
    base.update(overrides)
    return MCPServerSpec(**base)


def make_adapter(fake: FakeMCPClient, *, alias: str = "srv",
                 writable_tools: Optional[List[str]] = None) -> MCPWriteAdapter:
    return MCPWriteAdapter(
        alias=alias,
        spec=spec(alias=alias),
        writable_tools=writable_tools if writable_tools is not None else [t["name"] for t in fake.tools],
        client=fake,
    )


# ---------------------------------------------------------------------------------------------
# 1. Allowlist refusal -- zero network calls, spy-verified.
# ---------------------------------------------------------------------------------------------

def test_non_writable_tool_is_refused_and_never_reaches_call_tool():
    fake = FakeMCPClient(tools=[tool("comment"), tool("delete_everything")])
    adapter = make_adapter(fake, writable_tools=["comment"])  # delete_everything NOT writable
    result = adapter.write_operation("srv:delete_everything", {})
    assert result.ok is False
    assert "not in writable_tools" in result.error
    assert fake.call_tool_calls == []  # the spy: call_tool was NEVER invoked
    assert result.detail == {"tool": "delete_everything", "args": {}, "executed": False}


def test_read_allowlisting_grants_no_write_access():
    """A tool listed on a READ-side allowed_tools (not modeled here at all -- this adapter never
    even sees one) still requires its OWN, explicit writable_tools entry. Constructing the adapter
    with an empty writable_tools proves nothing is implicitly granted."""
    fake = FakeMCPClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=[])  # nothing writable at all
    result = adapter.write_operation("srv:comment", {"body": "hi"})
    assert result.ok is False
    assert "not in writable_tools" in result.error
    assert fake.call_tool_calls == []


def test_spec_writable_tools_used_as_default_when_not_passed_explicitly():
    fake = FakeMCPClient(tools=[tool("comment")])
    fake.call_tool_results["comment"] = MCPToolResult(ok=True, content="done")
    adapter = MCPWriteAdapter(alias="srv", spec=spec(writable_tools=["comment"]), client=fake)
    result = adapter.write_operation("comment", {"body": "hi"})
    assert result.ok is True


# ---------------------------------------------------------------------------------------------
# 2. Successful write -- ok=True, tool/args recorded for auditability.
# ---------------------------------------------------------------------------------------------

def test_writable_tool_call_succeeds_and_records_tool_and_args():
    fake = FakeMCPClient(tools=[tool("comment")])
    fake.call_tool_results["comment"] = MCPToolResult(ok=True, content="posted")
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("srv:comment", {"body": "hello"})
    assert result.ok is True
    assert result.rel_path == "srv:comment"
    assert fake.call_tool_calls == [("comment", {"body": "hello"})]
    assert result.detail == {
        "tool": "comment", "args": {"body": "hello"}, "executed": True, "content": "posted",
    }


def test_bare_tool_name_also_accepted_by_write_operation():
    """A caller that already knows the bare (non-aliased) tool name may pass it directly."""
    fake = FakeMCPClient(tools=[tool("comment")])
    fake.call_tool_results["comment"] = MCPToolResult(ok=True, content="posted")
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("comment", {"body": "hi"})
    assert result.ok is True
    assert fake.call_tool_calls == [("comment", {"body": "hi"})]


# ---------------------------------------------------------------------------------------------
# 3. Failing/erroring calls -- ok=False, never raises, still auditable.
# ---------------------------------------------------------------------------------------------

def test_tool_call_error_is_surfaced_never_raises():
    fake = FakeMCPClient(tools=[tool("comment")])
    fake.call_tool_results["comment"] = MCPToolResult(ok=False, error="rate limited")
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("srv:comment", {"body": "hi"})
    assert result.ok is False
    assert result.error == "rate limited"
    assert result.detail["tool"] == "comment"
    assert result.detail["executed"] is True


def test_args_must_be_a_dict():
    fake = FakeMCPClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("srv:comment", "not-a-dict")  # type: ignore[arg-type]
    assert result.ok is False
    assert "must be a dict" in result.error
    assert fake.call_tool_calls == []


def test_empty_tool_name_is_refused():
    fake = FakeMCPClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("", {})
    assert result.ok is False
    assert fake.call_tool_calls == []


def test_client_call_tool_raising_never_propagates():
    class RaisingClient(FakeMCPClient):
        def call_tool(self, name, args=None, *, timeout=None):
            raise RuntimeError("wire exploded")

    fake = RaisingClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("srv:comment", {"body": "hi"})  # must not raise
    assert result.ok is False
    assert "wire exploded" in result.error


# ---------------------------------------------------------------------------------------------
# 4. Discovery: list_writable_operations
# ---------------------------------------------------------------------------------------------

def test_list_writable_operations_filters_to_allowlist_and_aliases_names():
    fake = FakeMCPClient(tools=[
        tool("comment", "Post a comment", {"type": "object"}),
        tool("delete_everything", "Danger"),
    ])
    adapter = make_adapter(fake, writable_tools=["comment"])
    ops = adapter.list_writable_operations()
    assert ops == [{"name": "srv:comment", "description": "Post a comment", "input_schema": {"type": "object"}}]


def test_list_writable_operations_degrades_to_empty_on_discovery_failure():
    fake = ExplodingMCPClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=["comment"])
    assert adapter.list_writable_operations() == []


def test_list_writable_operations_empty_when_nothing_writable():
    fake = FakeMCPClient(tools=[tool("comment")])
    adapter = make_adapter(fake, writable_tools=[])
    assert adapter.list_writable_operations() == []


# ---------------------------------------------------------------------------------------------
# 5. Structural OperationWriter conformance
# ---------------------------------------------------------------------------------------------

def test_adapter_structurally_satisfies_operation_writer_protocol():
    adapter = make_adapter(FakeMCPClient(tools=[tool("comment")]), writable_tools=["comment"])
    assert isinstance(adapter, OperationWriter)


def test_write_operation_returns_a_write_result():
    fake = FakeMCPClient(tools=[tool("comment")])
    fake.call_tool_results["comment"] = MCPToolResult(ok=True, content="posted")
    adapter = make_adapter(fake, writable_tools=["comment"])
    result = adapter.write_operation("srv:comment", {})
    assert isinstance(result, WriteResult)


# ---------------------------------------------------------------------------------------------
# 6. Hard rule #1: no real hub/vendor/company name hardcoded in this adapter's own source.
# ---------------------------------------------------------------------------------------------

BANNED_REAL_NAMES = (
    "github", "gitlab", "bitbucket", "jira", "confluence", "slack", "notion", "salesforce",
    "zendesk", "asana", "trello", "linear.app", "hubspot", "anthropic", "spiritualdata",
    "quest-backend", "quest-frontend",
)


def test_no_real_vendor_name_hardcoded_in_adapter_source():
    src_path = Path(__file__).resolve().parents[1] / "quest_ai_runner" / "adapters" / "mcp_write_adapter.py"
    text = src_path.read_text(encoding="utf-8").lower()
    hits = [name for name in BANNED_REAL_NAMES if name in text]
    assert not hits, f"real vendor/org name(s) hardcoded in mcp_write_adapter.py: {hits}"
