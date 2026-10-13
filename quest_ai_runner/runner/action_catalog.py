"""Publish this runner's tools and connectors to Quest as workflow step actions.

Quest Workflows can run a step as "one action". Quest does not know this runner's tools: this module
tells it. It builds an action list from the runner's own definitions and PUTs it to Quest's generic
registration API (``PUT /api/teams/{id}/action-catalogs/{publisher}``, same ``qsk_`` key the runner
already polls with). Quest stores it and offers each entry in the workflow editor; when a step runs
one, Quest queues an ordinary task carrying ``requested_action`` and ``runner.requested_action``
executes it here.

WHAT IS PUBLISHED (a rule, not a list)
  * A tool is an action when it ACTS: ``ToolSpec.mutates`` is true. A read-only tool returns text that
    nothing in a scheduled step would consume, so it is plumbing, not a step. A tool can override the
    rule either way with ``workflow_step`` (``True`` publishes a read tool, ``False`` withholds a
    mutating one).
  * A connector (an MCP server in ``cfg.mcp_servers``) contributes each operation on its WRITE
    allowlist (``writable_tools``). The operator already opted those in as things this runner may do;
    read-side tools never appear.
Names, descriptions and parameters come from the tool's or the server's own definition.

WHEN IT IS PUBLISHED
  On the heartbeat the runner already sends, per team and org it serves. A publication is skipped
  while its content is unchanged and was sent within ``REPUBLISH_SECONDS``; Quest treats a catalog
  not refreshed for 48 hours as stale, so the periodic republish is the keep-alive.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.action_catalog")

PUBLISHER_ID = "quest-ai-runner"
PUBLISHER_LABEL = "Quest AI Runner"
REPUBLISH_SECONDS = 4 * 3600
KIND_TOOL = "tool"
KIND_CONNECTOR = "connector_operation"
TOOL_PREFIX = "tool:"
CONNECTOR_PREFIX = "connector:"


def readable(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").replace(":", ": ").strip().capitalize()


def is_publishable_tool(spec: Any) -> bool:
    """The documented rule: a standard tool that acts, unless the tool says otherwise. An
    operator-defined (custom) tool can do anything, outward mail included, so it is published only
    when its entry opts in with ``workflow_step = true``."""
    override = getattr(spec, "workflow_step", None)
    if override is not None:
        return bool(override)
    if getattr(spec, "origin", "custom") != "standard":
        return False
    return bool(getattr(spec, "mutates", True))


def params_schema_from_json_schema(schema: Optional[Dict[str, Any]], skip: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """A JSON-Schema object's properties as Quest's generic param fields. Object and array parameters
    become ``string`` fields holding JSON; an ``enum`` becomes a select. Parameters in ``skip`` (the
    ones the runner fills itself) are left out of the form."""
    skipped = set(skip)
    required = set((schema or {}).get("required") or [])
    fields: List[Dict[str, Any]] = []
    for name, prop in ((schema or {}).get("properties") or {}).items():
        if name in skipped or not isinstance(prop, dict):
            continue
        kind = prop.get("type")
        help_text = (prop.get("description") or "").strip() or None
        field: Dict[str, Any] = {"name": name, "label": readable(name), "required": name in required, "advanced": False}
        if isinstance(prop.get("enum"), list) and prop["enum"]:
            field["type"] = "enum"
            field["enum"] = [{"value": str(v), "label": str(v)} for v in prop["enum"]]
        elif kind in ("number", "integer"):
            field["type"] = "number"
        elif kind == "boolean":
            field["type"] = "boolean"
        else:
            field["type"] = "string"
            if kind in ("array", "object"):
                help_text = f"{help_text + ' ' if help_text else ''}(JSON {kind})"
        field["help"] = help_text
        fields.append(field)
    return fields


def tool_action(spec: Any) -> Dict[str, Any]:
    listing = spec.public_listing()
    description = listing["description"]
    if listing.get("when_to_use"):
        description = f"{description} Use when: {listing['when_to_use']}"
    return {
        "action_id": f"{TOOL_PREFIX}{spec.name}",
        "label": readable(spec.name),
        "description": f"{description} Changes data.".strip() if listing.get("mutates") else description.strip(),
        "actor_kinds": ["ai"],
        # Values the runner fills from the task (the quest, the task id, defaults) are not asked for.
        "params_schema": params_schema_from_json_schema(listing.get("parameters"), skip=[*(listing.get("auto_filled") or ()), *(getattr(spec, "workflow_hidden_params", None) or ())]),
        "request": {"kind": KIND_TOOL, "name": spec.name},
    }


def connector_actions(operations: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Actions for connector operations, as ``MCPWriteAdapter.list_writable_operations()`` lists them:
    ``[{name: "<alias>:<tool>", description, input_schema}]``."""
    out = []
    for op in operations:
        name = str(op.get("name") or "")
        if not name:
            continue
        alias = name.split(":", 1)[0]
        out.append({
            "action_id": f"{CONNECTOR_PREFIX}{name}",
            "label": readable(name),
            "description": f"Connector {alias}: {(op.get('description') or '').strip()} Changes data.".strip(),
            "actor_kinds": ["ai"],
            "params_schema": params_schema_from_json_schema(op.get("input_schema")),
            "request": {"kind": KIND_CONNECTOR, "name": name},
        })
    return out


def connector_operations(mcp_servers: Sequence[Any]) -> List[Dict[str, Any]]:
    """Every write-allowlisted operation across the configured MCP servers. Discovery is a network
    call per server; a server that cannot be reached contributes nothing this time."""
    ops: List[Dict[str, Any]] = []
    for spec in mcp_servers or []:
        if not getattr(spec, "writable_tools", None):
            continue
        try:
            from ..adapters.mcp_write_adapter import MCPWriteAdapter
            ops.extend(MCPWriteAdapter(alias=spec.alias, spec=spec, writable_tools=spec.writable_tools).list_writable_operations())
        except Exception:  # noqa: BLE001 -- one dead connector never stops the rest from publishing
            log.info("connector %s: operations not listed", getattr(spec, "alias", "?"), exc_info=True)
    return ops


def find_published_action(registry: Any, mcp_servers: Sequence[Any], request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """This runner's own published action matching a task's ``requested_action``, or None.

    Rebuilt from the runner's current definitions (never from the task), and ``action_id``, ``kind``
    and ``name`` must all match, so a task can only run something this runner offers right now.
    Connector discovery is limited to the one server the request names."""
    kind, name = request.get("kind"), str(request.get("name") or "")
    if kind == KIND_TOOL:
        spec = registry.get(name) if registry is not None else None
        candidates = [tool_action(spec)] if spec is not None and is_publishable_tool(spec) else []
    elif kind == KIND_CONNECTOR:
        alias = name.split(":", 1)[0]
        servers = [s for s in mcp_servers or [] if getattr(s, "alias", None) == alias]
        candidates = connector_actions(connector_operations(servers))
    else:
        return None
    for action in candidates:
        req = action.get("request") or {}
        if action["action_id"] == request.get("action_id") and req.get("kind") == kind and req.get("name") == name:
            return action
    return None


def build_actions(registry: Any, mcp_servers: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    actions = [tool_action(s) for s in sorted(registry.all(), key=lambda t: t.name) if is_publishable_tool(s)] if registry else []
    actions.extend(connector_actions(connector_operations(mcp_servers)))
    return actions


class CatalogPublisher:
    """Publishes the action list to each target (``("team", id)`` or ``("org", id)``), skipping a
    target whose content is unchanged and was published recently."""

    def __init__(self, clock=time.monotonic):
        self.sent: Dict[tuple, tuple] = {}
        self.clock = clock

    def publish(self, client: Any, targets: Sequence[tuple], actions: List[Dict[str, Any]], *,
                env_id: Optional[str] = None, label: Optional[str] = None) -> int:
        """Send to every target that needs it; returns how many were sent. Never raises."""
        digest = hashlib.sha256(json.dumps(actions, sort_keys=True).encode()).hexdigest()
        now = self.clock()
        sent = 0
        for target in targets:
            previous = self.sent.get(target)
            if previous and previous[0] == digest and now - previous[1] < REPUBLISH_SECONDS:
                continue
            try:
                result = client.publish_action_catalog(target[0], target[1], PUBLISHER_ID, actions,
                                                       env_id=env_id, publisher_label=label or PUBLISHER_LABEL)
            except Exception:  # noqa: BLE001 -- publishing must never break a heartbeat
                log.info("action catalog not published to %s", target, exc_info=True)
                continue
            if result:
                self.sent[target] = (digest, now)
                sent += 1
        return sent
