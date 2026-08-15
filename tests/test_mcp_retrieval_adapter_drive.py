"""Offline tests: ``MCPRetrievalAdapter`` against a SECOND real, distinct MCP hub -- Google's
Drive MCP server (``https://drivemcp.googleapis.com/mcp/v1``, HTTP transport, OAuth 2.0 bearer).

This is the genericity proof phase 1 was betting on: adding a second, structurally different real
MCP hub should cost config + a fixture, not new adapter code. This file adds NO changes to
``mcp_retrieval_adapter.py`` -- it only exercises the existing adapter against a Drive-shaped
``FakeMCPClient`` (the exact same fake/seam ``test_mcp_retrieval_adapter.py`` uses for its own
genericity table), reusing that file's ``FakeMCPClient``/``tool``/``resource`` helpers rather than
duplicating them.

SCHEMA PROVENANCE: the 8 tool definitions below (name, description, ``inputSchema``) are copied
from Google's own published MCP tool reference, fetched 2026-08-14:
  https://developers.google.com/workspace/drive/api/reference/mcp
  https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/<tool_name>
These are the LITERAL published schemas (not reconstructed placeholders), current as of the Drive
MCP server's Developer Preview at the time this file was written. If Google revises the published
schemas later, this fixture may drift from the live server -- it is not re-verified against a real
connection (this test suite is fully offline by design; see the module docstrings on
``mcp_client.py`` / ``mcp_retrieval_adapter.py``).

READ/WRITE SEPARATION: of the 8 published tools, ``create_file`` and ``copy_file`` are MUTATING
(create / duplicate a Drive file) and must NEVER be treated as read-only. This file proves that
separation holds for Drive's REAL tool set, not just a synthetic one: an adapter configured with
``allowed_tools`` = the other 6 (read-only) tools refuses both of them via ``query()``, the same
allowlist-refusal path ``test_mcp_retrieval_adapter.py`` already pins generically
(``test_non_allowlisted_tool_is_refused_and_never_reaches_call_tool``).

Hard rule #1 (this repo is public): no real Drive file id, folder id, or Workspace subject email
appears anywhere below -- only the tool/schema shapes Google publishes for anyone to read, plus
synthetic ids like ``"file-abc123"``.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from quest_ai_runner.adapters.mcp_client import MCPToolResult
from quest_ai_runner.adapters.mcp_retrieval_adapter import MCPRetrievalAdapter
from tests.test_mcp_retrieval_adapter import FakeMCPClient, make_adapter, tool

# ---------------------------------------------------------------------------------------------
# The real 8-tool Drive MCP surface (literal published schemas -- see module docstring).
# ---------------------------------------------------------------------------------------------

DRIVE_TOOLS = [
    tool(
        "search_files",
        "Search for Drive files using structured query syntax combining query terms (title, "
        "fullText, mimeType, modifiedTime, viewedByMeTime, createdTime, parentId, owner, "
        "sharedWithMe) with operators (contains, =, !=, <, <=, >, >=) and logical connectors "
        "(and, or, not). Supports pagination via next_page_token.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "pageToken": {"type": "string", "description": "The page token to use for pagination"},
                "pageSize": {"type": "integer", "description": "The maximum number of files to return in each page"},
                "excludeContentSnippets": {
                    "type": "boolean",
                    "description": "If true, the content snippet will be excluded from the response",
                },
            },
            "required": ["query"],
        },
    ),
    tool(
        "read_file_content",
        "Call this tool to fetch a natural language representation of a Drive file, and "
        "optionally, its comments.",
        {
            "type": "object",
            "properties": {
                "fileId": {"type": "string", "description": "Required. The ID of the file to retrieve."},
                "includeComments": {
                    "type": "boolean",
                    "description": "Whether to include comments in the response.",
                },
            },
            "required": ["fileId"],
        },
    ),
    tool(
        "download_file_content",
        "Call this tool to download the content of a Drive file as a base64 encoded string.",
        {
            "type": "object",
            "properties": {
                "fileId": {"type": "string", "description": "Required. The ID of the file to retrieve."},
                "exportMimeType": {
                    "type": "string",
                    "description": "Optional. For Google native files, the MIME type to export the "
                    "file to, ignored otherwise. Defaults to text if not specified.",
                },
            },
            "required": ["fileId"],
        },
    ),
    tool(
        "get_file_metadata",
        "Call this tool to find general metadata about a user's Drive file. If the file is not "
        "found, try using other tools like search_files to find the file the user is requesting.",
        {
            "type": "object",
            "properties": {
                "fileId": {"type": "string", "description": "Required. The ID of the file to retrieve."},
                "excludeContentSnippets": {
                    "type": "boolean",
                    "description": "If true, the content snippet will be excluded from the response.",
                },
            },
            "required": ["fileId"],
        },
    ),
    tool(
        "get_file_permissions",
        "Call this tool to list the permissions of a Drive File.",
        {
            "type": "object",
            "properties": {
                "fileId": {"type": "string", "description": "Required. The ID of the file to get permissions for."},
            },
            "required": ["fileId"],
        },
    ),
    tool(
        "list_recent_files",
        "Call this tool to find recent files for a user specified a sort order. Default sort "
        "order is recency.",
        {
            "type": "object",
            "properties": {
                "orderBy": {"type": "string", "description": "The sort order for the files"},
                "pageToken": {"type": "string", "description": "The page token to use for pagination"},
                "pageSize": {"type": "integer", "description": "The maximum number of files to return"},
                "excludeContentSnippets": {
                    "type": "boolean",
                    "description": "If true, the content snippet will be excluded from the response",
                },
            },
        },
    ),
    # --- MUTATING tools. Real, published, but MUST stay out of any read-only allowlist. ---
    tool(
        "create_file",
        "Call this tool to create or upload a File to Google Drive.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The title of the file"},
                "contentMimeType": {
                    "type": "string",
                    "description": "The MIME type of the content being uploaded. Required when "
                    "content is provided",
                },
                "base64Content": {
                    "type": "string",
                    "description": "Optional. Base64 encoded content. Cannot be set with textContent",
                },
                "textContent": {
                    "type": "string",
                    "description": "Optional. UTF-8 text content. Cannot be set with base64Content",
                },
                "parentId": {"type": "string", "description": "The parent folder ID of the file"},
                "disableConversionToGoogleType": {
                    "type": "boolean",
                    "description": "Set true to retain the content MIME type without conversion to "
                    "Google equivalents",
                },
            },
            "required": ["title"],
        },
    ),
    tool(
        "copy_file",
        "Duplicates an existing file in Google Drive with optional custom title and destination "
        "folder. If the title is not specified, the copy title will be 'Copy of {original title}'. "
        "If the parent folder is not specified, the copy will be created in the same folder as the "
        "original file.",
        {
            "type": "object",
            "properties": {
                "fileId": {"type": "string", "description": "Required. The ID of the file to copy."},
                "title": {
                    "type": "string",
                    "description": "The title of the newly created file. If empty, defaults to "
                    "'Copy of [original file title]'.",
                },
                "parentId": {
                    "type": "string",
                    "description": "The parent folder ID for the new file. If empty, uses the "
                    "original file's parent folder.",
                },
            },
            "required": ["fileId"],
        },
    ),
]

READ_ONLY_DRIVE_TOOLS = [
    "search_files",
    "read_file_content",
    "download_file_content",
    "get_file_metadata",
    "get_file_permissions",
    "list_recent_files",
]

MUTATING_DRIVE_TOOLS = ["create_file", "copy_file"]

# Drive's real MCP surface publishes no `resources/list` -- it is purely tool-shaped (search +
# call), unlike e.g. a resources-only or both-shaped fixture. FakeMCPClient defaults resources=[].


def drive_fake_client(*, call_tool_results: Dict[str, MCPToolResult] = None) -> FakeMCPClient:
    """A FakeMCPClient scripted with the full real 8-tool Drive surface and no resources."""
    return FakeMCPClient(tools=list(DRIVE_TOOLS), resources=[], call_tool_results=call_tool_results or {})


def drive_readonly_adapter(fake: FakeMCPClient) -> MCPRetrievalAdapter:
    """The adapter as this org would actually configure it: alias 'drive', allowlist = the 6
    read-only tools only. create_file/copy_file are NEVER in this list."""
    return make_adapter(fake, alias="drive", allowed_tools=list(READ_ONLY_DRIVE_TOOLS))


# ---------------------------------------------------------------------------------------------
# 1. Genericity: same adapter class, same assertions, against Drive's REAL tool set.
# ---------------------------------------------------------------------------------------------

def test_list_operations_surfaces_all_8_tools_namespaced():
    """list_operations() enumerates the full real 8-tool surface, namespaced under 'drive:' -- the
    mutating two are still LISTED (discoverable) but marked not callable, exactly like the
    synthetic 'delete_everything' case in test_mcp_retrieval_adapter.py."""
    fake = drive_fake_client()
    adapter = drive_readonly_adapter(fake)

    obs = adapter.list_operations()
    assert obs.kind != "error"
    lines_by_tool = {
        line.split(":", 2)[1]: line
        for line in obs.text.splitlines()
        if line.startswith("drive:")
    }
    for name in READ_ONLY_DRIVE_TOOLS:
        assert name in lines_by_tool, f"{name} missing from list_operations() output"
        assert "[not callable" not in lines_by_tool[name]
    for name in MUTATING_DRIVE_TOOLS:
        assert name in lines_by_tool, f"{name} missing from list_operations() output"
        assert "[not callable: outside allowed_tools]" in lines_by_tool[name]


@pytest.mark.parametrize("tool_name", READ_ONLY_DRIVE_TOOLS)
def test_describe_operation_renders_real_schema_for_each_readonly_tool(tool_name):
    fake = drive_fake_client()
    adapter = drive_readonly_adapter(fake)
    obs = adapter.describe_operation(f"drive:{tool_name}")
    assert obs.kind != "error"
    assert tool_name in obs.text
    assert "callable via query(): yes" in obs.text


def test_search_files_query_succeeds_and_reaches_call_tool_with_real_args():
    fake = drive_fake_client(call_tool_results={
        "search_files": MCPToolResult(ok=True, content='{"files": [{"id": "file-abc123", "title": "Q3 plan"}]}'),
    })
    adapter = drive_readonly_adapter(fake)
    result = adapter.query({
        "tool": "drive:search_files",
        "args": {"query": "title contains 'Q3 plan'", "pageSize": 10},
    })
    assert result.kind == "query"
    assert "Q3 plan" in result.text
    assert fake.call_tool_calls == [("search_files", {"query": "title contains 'Q3 plan'", "pageSize": 10})]


def test_read_file_content_query_succeeds():
    fake = drive_fake_client(call_tool_results={
        "read_file_content": MCPToolResult(ok=True, content="This is the plain-text body of the file."),
    })
    adapter = drive_readonly_adapter(fake)
    result = adapter.query({"tool": "drive:read_file_content", "args": {"fileId": "file-abc123"}})
    assert result.kind == "query"
    assert "plain-text body" in result.text


def test_get_file_metadata_missing_required_fileid_surfaces_real_schema():
    """A caller that omits the required 'fileId' arg gets an error whose message includes the
    TOOL'S OWN real schema (from Google's published inputSchema), so the orchestrator can replan
    with the right argument name."""
    fake = drive_fake_client(call_tool_results={
        "get_file_metadata": MCPToolResult(ok=False, error="missing required argument: fileId"),
    })
    adapter = drive_readonly_adapter(fake)
    result = adapter.query({"tool": "drive:get_file_metadata", "args": {}})
    assert result.kind == "error"
    assert "missing required argument" in result.error
    assert '"fileId"' in result.error
    assert "required" in result.error


# ---------------------------------------------------------------------------------------------
# 2. Read/write separation on the REAL tool set: create_file / copy_file refused when not
#    allowlisted, and MCPClient.call_tool is never even reached for them (spy assertion).
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mutating_tool", MUTATING_DRIVE_TOOLS)
def test_mutating_drive_tool_is_refused_when_not_in_readonly_allowlist(mutating_tool):
    fake = drive_fake_client()
    adapter = drive_readonly_adapter(fake)  # allowlist is ONLY the 6 read-only tools

    result = adapter.query({"tool": f"drive:{mutating_tool}", "args": {"title": "whatever"}})

    assert result.kind == "error"
    assert "not in allowed_tools" in result.error
    assert mutating_tool in result.error
    # The spy: the mutating call NEVER reached the underlying client, let alone a real server.
    assert fake.call_tool_calls == []


def test_create_file_would_succeed_if_misconfigured_into_the_allowlist():
    """Negative control: proves the refusal above is actually the allowlist gate doing its job,
    not e.g. a hardcoded name check -- an adapter that WERE (wrongly) configured with create_file
    allowed does call through. This is exactly why create_file/copy_file must never be placed in
    a read-only allowed_tools list in real config (see the consumer wiring)."""
    fake = drive_fake_client(call_tool_results={
        "create_file": MCPToolResult(ok=True, content='{"id": "file-new1", "title": "whatever"}'),
    })
    misconfigured_adapter = make_adapter(fake, alias="drive", allowed_tools=["create_file"])
    result = misconfigured_adapter.query({"tool": "drive:create_file", "args": {"title": "whatever"}})
    assert result.kind == "query"
    assert fake.call_tool_calls == [("create_file", {"title": "whatever"})]


# ---------------------------------------------------------------------------------------------
# 3. No-resources shape: Drive's real surface exposes no MCP resources at all (pure tool/call
#    server), which is itself a genericity data point distinct from the synthetic
#    tools_only/resources_only/both fixtures in test_mcp_retrieval_adapter.py.
# ---------------------------------------------------------------------------------------------

def test_list_sources_on_a_tools_only_real_server_is_an_honest_empty_report():
    fake = drive_fake_client()
    adapter = drive_readonly_adapter(fake)
    obs = adapter.list_sources()
    assert obs.kind != "error"
    assert "no" in obs.text.lower()


def test_grep_is_honestly_unsupported_for_drive_too():
    fake = drive_fake_client()
    adapter = drive_readonly_adapter(fake)
    result = adapter.grep("anything")
    assert result.kind == "error"
    assert "not supported" in result.error.lower()
