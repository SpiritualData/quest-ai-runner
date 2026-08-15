"""MCPRetrievalAdapter -- a ``RetrievalAdapter`` over ONE generic MCP (Model Context Protocol)
server, satisfying the standard discovery quartet + read/query surface every other source in this
repo uses.

Maps QAR's discovery quartet onto MCP's own discovery calls:

  * ``list_sources``       -> MCP ``resources/list``  (``MCPClient.list_resources``)
  * ``describe_source``    -> that resource's metadata
  * ``list_operations``    -> MCP ``tools/list``       (``MCPClient.list_tools``)
  * ``describe_operation``  -> that tool's ``inputSchema``, rendered as text
  * ``read_section``       -> MCP ``resources/read``   (``MCPClient.read_resource``), truncated to
                              a configurable ``max_bytes``
  * ``query({"tool": name, "args": {...}})`` -> MCP ``tools/call`` (``MCPClient.call_tool``), GATED
                              by an explicit allowlist of tool names -- a tool not on the allowlist
                              is refused with a clear error Observation; ``MCPClient.call_tool`` is
                              never even reached for it.
  * ``grep``                -> NOT SUPPORTED. MCP has no analogue; this returns an honest
                              ``Observation(kind="error", ...)`` rather than faking a scan.

NAMESPACING (mandatory). Every source/operation name this adapter surfaces is prefixed with its
configured ``alias`` (e.g. an adapter configured with ``alias="issues"`` surfaces a ``search`` tool
as ``issues:search``), so two different MCP servers exposing a same-named tool never collide when
several ``MCPRetrievalAdapter``s sit inside the same
``CompositeRetrievalAdapter``. The namespacing is enforced at the LOOKUP boundary
(``describe_source``/``describe_operation``/``read_section``/``query`` all refuse a name that does
not start with ``"{alias}:"``), which is what actually keeps two aliased adapters distinguishable
through ``CompositeRetrievalAdapter``'s per-adapter delegation (it tries each adapter in turn and
keeps the first non-error answer) -- not merely a naming convention on the enumeration text.

This module is READ-ONLY by construction (Phase 1 / foundation scope): there is no write path here
and none is planned for this adapter. A write-capable MCP adapter is explicitly out of scope for
this phase.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase
from .mcp_client import MCPClient, MCPServerSpec

_log = logging.getLogger("quest-ai-runner.mcp_retrieval_adapter")

# Matches FilesAdapter's default_read_max_bytes -- the repo-wide convention for a single read.
DEFAULT_MAX_BYTES = 20_000


class MCPRetrievalAdapter(RetrievalAdapterBase):
    """RetrievalAdapter over one MCP server, namespaced by ``alias``.

    Args:
        alias: Namespace prefix for every source/operation name this adapter surfaces (e.g.
            ``"issues"`` -> ``"issues:search"``). Authoritative for THIS adapter instance
            (independent of ``spec.alias``, so a consumer or a test can wrap one connection spec
            in adapters under different aliases without cloning the spec).
        spec: The ``MCPServerSpec`` connection spec handed to the underlying ``MCPClient``.
        allowed_tools: Read-only tool NAMES (bare, as the server names them -- not aliased) this
            adapter is permitted to invoke via ``query()``. A tool not in this list is refused;
            ``MCPClient.call_tool`` is never called for it. Defaults to ``spec.allowed_tools`` when
            not given explicitly.
        max_bytes: Default truncation for ``read_section`` / ``query`` results.
        timeout_s: Per-call timeout passed to ``MCPClient``. Defaults to ``spec.timeout_s``.
    """

    def __init__(
        self,
        *,
        alias: str,
        spec: MCPServerSpec,
        allowed_tools: Optional[List[str]] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_s: Optional[float] = None,
        client: Optional[MCPClient] = None,
    ) -> None:
        self._alias = str(alias or "").strip()
        if not self._alias:
            raise ValueError("MCPRetrievalAdapter requires a non-empty alias")
        self._spec = spec
        self._allowed_tools = set(allowed_tools if allowed_tools is not None else (spec.allowed_tools or []))
        self._max_bytes = int(max_bytes)
        self._timeout_s = float(timeout_s) if timeout_s is not None else float(spec.timeout_s or 30.0)
        # Injectable for tests; defaults to a real client wired to ``spec``.
        self._client = client if client is not None else MCPClient(spec)

    @property
    def alias(self) -> str:
        return self._alias

    # ------------------------------------------------------------------
    # Namespacing helpers
    # ------------------------------------------------------------------

    def _prefix(self) -> str:
        return f"{self._alias}:"

    def _strip_alias(self, name: Any) -> Optional[str]:
        """The bare id with this adapter's alias prefix removed, or None if ``name`` does not
        belong to this adapter (the namespacing refusal boundary). Never raises."""
        text = str(name or "")
        prefix = self._prefix()
        if text.startswith(prefix):
            return text[len(prefix):]
        return None

    def _bare_tool_name(self, ref: Any) -> str:
        """Accept either an aliased (``"alias:tool"``) or bare (``"tool"``) tool reference for
        ``query()`` -- a planner that just discovered the name via ``list_operations()`` passes the
        aliased form; a caller that already knows the bare name may pass that directly."""
        text = str(ref or "").strip()
        stripped = self._strip_alias(text)
        return stripped if stripped is not None else text

    # ------------------------------------------------------------------
    # RetrievalAdapter: read / grep / query
    # ------------------------------------------------------------------

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        try:
            uri = self._strip_alias(rel_path)
            if uri is None:
                return Observation(
                    kind="error", rel_path=rel_path,
                    error=f"{rel_path!r} is not a resource of {self._alias!r} (expected {self._prefix()}<uri>)",
                )
            result = self._client.read_resource(uri, timeout=self._timeout_s)
            if not result.ok:
                return Observation(kind="error", rel_path=rel_path,
                                    error=result.error or f"MCP resource read failed: {uri!r}")
            text = result.content
            if start_line or end_line:
                lines = text.split("\n")
                start = (start_line or 1) - 1
                end = end_line or len(lines)
                text = "\n".join(lines[max(0, start): min(len(lines), end)])
            effective_max = max_bytes if max_bytes is not None else self._max_bytes
            if effective_max and len(text) > effective_max:
                text = text[:effective_max].rsplit("\n", 1)[0] + "\n[truncated]"
            return Observation(kind="read", rel_path=rel_path, text=text)
        except Exception as exc:  # noqa: BLE001
            _log.debug("mcp read_section failed for %r: %s", rel_path, exc)
            return Observation(kind="error", rel_path=rel_path, error=f"mcp read error: {exc}")

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """MCP has no grep/search analogue. Honest refusal -- never a faked scan."""
        return Observation(kind="error", pattern=pattern, error="grep is not supported by MCP sources")

    def query(self, spec: Dict[str, Any]) -> Observation:
        """``{"tool": "<alias:name or name>", "args": {...}}`` -> ``tools/call``, ALLOWLIST-gated.

        Refuses (never calls ``MCPClient.call_tool``) when the tool is not in ``allowed_tools``. On
        a tool-call failure, appends the tool's own input schema (when discoverable) to the error so
        the orchestrator can replan with corrected arguments.
        """
        try:
            tool_ref = spec.get("tool") or spec.get("name")
            if not tool_ref:
                return Observation(kind="error", error="MCPRetrievalAdapter.query requires spec['tool']")
            tool_name = self._bare_tool_name(tool_ref)
            if not tool_name:
                return Observation(kind="error", error="MCPRetrievalAdapter.query: empty tool name")
            if tool_name not in self._allowed_tools:
                return Observation(
                    kind="error",
                    error=(
                        f"MCP tool {tool_name!r} is not in allowed_tools for {self._alias!r}; "
                        f"refusing to call it. Allowed: {sorted(self._allowed_tools)}"
                    ),
                )
            args = spec.get("args") or {}
            if not isinstance(args, dict):
                return Observation(kind="error", error="MCPRetrievalAdapter.query: spec['args'] must be a dict")

            result = self._client.call_tool(tool_name, args, timeout=self._timeout_s)
            if not result.ok:
                error = result.error or f"MCP tool call failed: {tool_name!r}"
                schema_hint = self._schema_hint(tool_name)
                if schema_hint:
                    error = f"{error} | expected schema: {schema_hint}"
                return Observation(kind="error", error=error)

            text = result.content
            if self._max_bytes and len(text) > self._max_bytes:
                text = text[: self._max_bytes].rsplit("\n", 1)[0] + "\n[truncated]"
            return Observation(kind="query", text=text, rel_path=f"{self._alias}:{tool_name}")
        except Exception as exc:  # noqa: BLE001
            _log.debug("mcp query failed: %s", exc)
            return Observation(kind="error", error=f"mcp query error: {exc}")

    def _schema_hint(self, tool_name: str) -> Optional[str]:
        """Best-effort: the tool's own inputSchema, rendered compactly, for a failed call's error
        message. Never raises; returns None if the schema cannot be found."""
        try:
            for tool in self._client.list_tools():
                if tool.get("name") == tool_name:
                    schema = tool.get("input_schema") or {}
                    return json.dumps(schema, default=str)
        except Exception:  # noqa: BLE001
            pass
        return None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        try:
            resources = self._client.list_resources()
            if not resources:
                return Observation(
                    kind="query", locator="list_sources",
                    text=f"No MCP resources exposed by {self._alias!r} (resources/list returned none).",
                )
            lines = []
            for r in resources:
                uri = r.get("uri")
                if not uri:
                    continue
                label = r.get("name") or uri
                desc = r.get("description") or ""
                suffix = f" - {desc}" if desc else ""
                lines.append(f"{self._alias}:{uri}: {label}{suffix}")
            if not lines:
                return Observation(kind="query", locator="list_sources",
                                    text=f"No addressable MCP resources exposed by {self._alias!r}.")
            return Observation(kind="query", locator="list_sources", text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"mcp list_sources error: {exc}")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        try:
            uri = self._strip_alias(name)
            if uri is None:
                return Observation(kind="error", error=f"{name!r} is not a source of {self._alias!r}")
            match = next((r for r in self._client.list_resources() if r.get("uri") == uri), None)
            if match is None:
                return Observation(kind="error", error=f"MCP resource not found on {self._alias!r}: {uri!r}")
            mime = match.get("mime_type") or "unknown mime type"
            desc = match.get("description") or match.get("name") or ""
            return Observation(
                kind="query", locator=f"describe_source({name})",
                text=f"{self._alias}:{uri} ({mime}): {desc}",
            )
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"mcp describe_source error: {exc}")

    def list_operations(self) -> Observation:
        try:
            tools = self._client.list_tools()
            if not tools:
                return Observation(
                    kind="query", locator="list_operations",
                    text=f"No MCP operations exposed by {self._alias!r} (tools/list returned none).",
                )
            lines = []
            for t in tools:
                name = t.get("name")
                if not name:
                    continue
                desc = t.get("description") or ""
                marker = "" if name in self._allowed_tools else " [not callable: outside allowed_tools]"
                lines.append(f"{self._alias}:{name}: {desc}{marker}")
            if not lines:
                return Observation(kind="query", locator="list_operations",
                                    text=f"No named MCP operations exposed by {self._alias!r}.")
            return Observation(kind="query", locator="list_operations", text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"mcp list_operations error: {exc}")

    def describe_operation(self, name: str) -> Observation:
        try:
            tool_name = self._strip_alias(name)
            if tool_name is None:
                return Observation(kind="error", error=f"{name!r} is not an operation of {self._alias!r}")
            match = next((t for t in self._client.list_tools() if t.get("name") == tool_name), None)
            if match is None:
                return Observation(kind="error", error=f"MCP tool not found on {self._alias!r}: {tool_name!r}")
            schema = match.get("input_schema") or {}
            allowed = tool_name in self._allowed_tools
            text = (
                f"{self._alias}:{tool_name}: {match.get('description') or ''}\n"
                f"input_schema: {json.dumps(schema, indent=2, default=str)}\n"
                f"callable via query(): {'yes' if allowed else 'no (not in allowed_tools)'}"
            )
            return Observation(kind="query", locator=f"describe_operation({name})", text=text)
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"mcp describe_operation error: {exc}")
