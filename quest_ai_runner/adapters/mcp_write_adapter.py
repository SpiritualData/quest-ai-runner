"""MCPWriteAdapter -- an ``OperationWriter`` over ONE generic MCP (Model Context Protocol)
server's MUTATING tools.

Maps QAR's write side onto MCP's own ``tools/list`` (discovery) + ``tools/call`` (execution),
mirroring ``mcp_retrieval_adapter.MCPRetrievalAdapter``'s relationship to ``MCPClient`` exactly,
but for the ``OperationWriter`` interface (``core/adapters.py``) instead of ``RetrievalAdapter``:

  * ``list_writable_operations()`` -> MCP ``tools/list`` (``MCPClient.list_tools``), filtered to
                              this adapter's own ``writable_tools`` allowlist and rendered as
                              ``{name, description, input_schema}`` with the name ALIAS-prefixed
                              (matching ``MCPRetrievalAdapter``'s namespacing).
  * ``write_operation(name, args)`` -> MCP ``tools/call`` (``MCPClient.call_tool``), GATED by an
                              explicit allowlist of tool names -- a tool not on the allowlist is
                              refused with a clear ``WriteResult(ok=False)`` and
                              ``MCPClient.call_tool`` is never even reached for it.

``writable_tools`` IS A SEPARATE ALLOWLIST from ``MCPRetrievalAdapter.allowed_tools`` (the read
side). A tool listed there for read-side ``query()`` grants NOTHING here: it must be listed
again, explicitly, in THIS adapter's ``writable_tools`` before ``write_operation`` will ever call
it. Two adapters (an ``MCPRetrievalAdapter`` and an ``MCPWriteAdapter``) may share the same
``MCPServerSpec``/``MCPClient`` connection while enforcing entirely independent policies -- one
spec, two allowlists, two adapter instances.

NAMESPACING (mandatory), same convention as the read adapter: every operation name this adapter
surfaces or accepts is prefixed with its configured ``alias`` (e.g. an adapter configured with
``alias="issues"`` surfaces a ``create`` tool as ``issues:create``), so two different MCP servers
exposing a same-named tool never collide when several write adapters coexist in a consumer's
wiring. ``write_operation`` accepts either the aliased or the bare form (a planner that just
discovered the name via ``list_writable_operations()`` passes the aliased form).

AUDITABILITY. Every ``write_operation`` call -- refused, failed, or successful -- returns a
``WriteResult`` whose ``detail`` dict records ``{"tool": <bare tool name>, "args": <the args>,
"executed": <bool>}`` (plus the tool's raw ``content`` on a successful/attempted call), so a
caller can always tell exactly what was or was not run, even when ``ok=False``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.adapters import OperationWriterBase, WriteResult
from .mcp_client import MCPClient, MCPServerSpec

_log = logging.getLogger("quest-ai-runner.mcp_write_adapter")


class MCPWriteAdapter(OperationWriterBase):
    """OperationWriter over one MCP server's mutating tools, namespaced by ``alias``.

    Args:
        alias: Namespace prefix for every operation name this adapter surfaces or accepts (e.g.
            ``"issues"`` -> ``"issues:create"``). Authoritative for THIS adapter instance
            (independent of ``spec.alias``, so a consumer or a test can wrap one connection spec
            in adapters under different aliases/policies without cloning the spec).
        spec: The ``MCPServerSpec`` connection spec handed to the underlying ``MCPClient``. May be
            the SAME spec instance an ``MCPRetrievalAdapter`` for this server also uses; the two
            adapters' allowlists stay independent regardless.
        writable_tools: WRITE-allowlisted tool NAMES (bare, as the server names them -- not
            aliased) this adapter is permitted to invoke via ``write_operation()``. A tool not in
            this list is refused; ``MCPClient.call_tool`` is never called for it. Defaults to
            ``spec.writable_tools`` when not given explicitly. Distinct and separate from any
            read-side ``allowed_tools`` -- being read-allowlisted grants nothing here.
        timeout_s: Per-call timeout passed to ``MCPClient``. Defaults to ``spec.timeout_s``.
        client: Injectable for tests; defaults to a real ``MCPClient`` wired to ``spec``.
    """

    def __init__(
        self,
        *,
        alias: str,
        spec: MCPServerSpec,
        writable_tools: Optional[List[str]] = None,
        timeout_s: Optional[float] = None,
        client: Optional[MCPClient] = None,
    ) -> None:
        self._alias = str(alias or "").strip()
        if not self._alias:
            raise ValueError("MCPWriteAdapter requires a non-empty alias")
        self._spec = spec
        self._writable_tools = set(
            writable_tools if writable_tools is not None else (getattr(spec, "writable_tools", None) or [])
        )
        self._timeout_s = float(timeout_s) if timeout_s is not None else float(spec.timeout_s or 30.0)
        # Injectable for tests; defaults to a real client wired to ``spec``.
        self._client = client if client is not None else MCPClient(spec)

    @property
    def alias(self) -> str:
        return self._alias

    @property
    def writable_tools(self) -> List[str]:
        return sorted(self._writable_tools)

    # ------------------------------------------------------------------
    # Namespacing helpers -- same shape as MCPRetrievalAdapter's.
    # ------------------------------------------------------------------

    def _prefix(self) -> str:
        return f"{self._alias}:"

    def _bare_tool_name(self, ref: Any) -> str:
        """Accept either an aliased (``"alias:tool"``) or bare (``"tool"``) tool reference for
        ``write_operation()`` -- a planner that just discovered the name via
        ``list_writable_operations()`` passes the aliased form; a caller that already knows the
        bare name may pass that directly."""
        text = str(ref or "").strip()
        prefix = self._prefix()
        return text[len(prefix):] if text.startswith(prefix) else text

    # ------------------------------------------------------------------
    # OperationWriter: list_writable_operations / write_operation
    # ------------------------------------------------------------------

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        """``[{"name": "<alias>:<tool>", "description", "input_schema"}, ...]`` for every tool on
        this adapter's ``writable_tools`` allowlist that the server actually exposes. Best-effort:
        a discovery failure (dead connection, timeout) is logged and returns ``[]`` rather than
        raising -- exactly ``MCPClient.list_tools``'s own degradation contract. A configured but
        undiscoverable tool name simply does not appear (nothing to describe it with); it is still
        refused honestly by ``write_operation`` if named directly.
        """
        try:
            tools = self._client.list_tools()
        except Exception as exc:  # noqa: BLE001 -- discovery is best-effort, never fatal here
            _log.debug("mcp list_writable_operations failed for %r: %s", self._alias, exc)
            return []
        out: List[Dict[str, Any]] = []
        for t in tools:
            name = t.get("name")
            if not name or name not in self._writable_tools:
                continue
            out.append({
                "name": f"{self._alias}:{name}",
                "description": t.get("description") or "",
                "input_schema": t.get("input_schema") or {},
            })
        return out

    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult:
        """``{"tool": name, "args": args}`` -> ``tools/call``, WRITE-ALLOWLIST-gated.

        Refuses (never calls ``MCPClient.call_tool``, never even touches the network) when the
        tool is not in ``writable_tools``. Every outcome -- refused, failed, or successful --
        carries the attempted ``tool``/``args`` in ``WriteResult.detail`` for auditability.
        """
        try:
            tool_name = self._bare_tool_name(name)
            if not tool_name:
                return WriteResult(
                    ok=False, rel_path=str(name or ""),
                    error="MCPWriteAdapter.write_operation: empty tool name",
                    detail={"tool": tool_name, "args": args, "executed": False},
                )
            target = f"{self._alias}:{tool_name}"
            if tool_name not in self._writable_tools:
                return WriteResult(
                    ok=False, rel_path=target,
                    error=(
                        f"MCP tool {tool_name!r} is not in writable_tools for {self._alias!r}; "
                        f"refusing to call it. Writable: {sorted(self._writable_tools)}"
                    ),
                    detail={"tool": tool_name, "args": args, "executed": False},
                )
            if not isinstance(args, dict):
                return WriteResult(
                    ok=False, rel_path=target,
                    error="MCPWriteAdapter.write_operation: args must be a dict",
                    detail={"tool": tool_name, "args": args, "executed": False},
                )

            result = self._client.call_tool(tool_name, args, timeout=self._timeout_s)
            detail = {"tool": tool_name, "args": args, "executed": True, "content": result.content}
            if not result.ok:
                return WriteResult(
                    ok=False, rel_path=target,
                    error=result.error or f"MCP tool call failed: {tool_name!r}",
                    detail=detail,
                )
            return WriteResult(ok=True, rel_path=target, detail=detail)
        except Exception as exc:  # noqa: BLE001 -- an OperationWriter must never raise
            _log.debug("mcp write_operation failed: %s", exc)
            return WriteResult(
                ok=False, rel_path=str(name or ""), error=f"mcp write error: {exc}",
                detail={"tool": name, "args": args, "executed": False},
            )
