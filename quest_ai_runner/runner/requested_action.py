"""Run the one action a Quest workflow step asked for, without a model.

A task Quest queues for a published action carries ``requested_action``:
``{publisher, kind, name, action_id, args}`` (see ``runner.action_catalog`` for what is published).
This module calls exactly that tool or connector operation with exactly those arguments and returns
the outcome. There is no planner in between, so the step does what it says and nothing else. The
poller reports the outcome (done or failed) through the usual task report calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from ..core.tools import ToolContext
from .action_catalog import KIND_CONNECTOR, KIND_TOOL

log = logging.getLogger("quest-ai-runner.requested_action")


@dataclass
class ActionOutcome:
    ok: bool
    text: str


def decode_json_params(properties: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Array and object parameters are entered in Quest as JSON text; decode them to real values."""
    out = dict(args)
    for name, value in args.items():
        kind = ((properties or {}).get(name) or {}).get("type")
        if kind in ("array", "object") and isinstance(value, str):
            try:
                out[name] = json.loads(value)
            except ValueError:
                pass  # leave it; the tool reports a clear error for a value it cannot use
        elif kind in ("number", "integer") and isinstance(value, str):
            try:
                out[name] = float(value) if "." in value else int(value)
            except ValueError:
                pass
    return out


def run_requested_action(task: Dict[str, Any], registry: Any, mcp_servers: Sequence[Any] = ()) -> ActionOutcome:
    request = task.get("requested_action") or {}
    kind, name = request.get("kind"), str(request.get("name") or "")
    args = dict(request.get("args") or {})
    if kind == KIND_TOOL:
        spec = registry.get(name) if registry is not None else None
        if spec is None:
            return ActionOutcome(False, f"This runner has no tool named {name!r}.")
        ctx = ToolContext(quest_id=str(task.get("goal_id") or "") or None,
                          user_id=str(task.get("user_id") or "") or None,
                          task_id=str(task.get("task_id") or task.get("id") or "") or None)
        result = registry.invoke(name, decode_json_params(spec.properties(), args), ctx)
        return ActionOutcome(result.ok, result.text or ("done" if result.ok else f"{name} failed"))
    if kind == KIND_CONNECTOR:
        alias = name.split(":", 1)[0]
        server = next((s for s in mcp_servers or [] if getattr(s, "alias", None) == alias), None)
        if server is None or not getattr(server, "writable_tools", None):
            return ActionOutcome(False, f"This runner has no write-enabled connector {alias!r}.")
        from ..adapters.mcp_write_adapter import MCPWriteAdapter
        written = MCPWriteAdapter(alias=server.alias, spec=server, writable_tools=server.writable_tools).write_operation(name, args)
        if written.ok:
            return ActionOutcome(True, f"Ran {name}.")
        return ActionOutcome(False, written.error or f"{name} failed")
    return ActionOutcome(False, f"This runner does not know how to run an action of kind {kind!r}.")
