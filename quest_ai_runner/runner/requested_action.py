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
from .action_catalog import KIND_TOOL, PUBLISHER_ID, find_published_action

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


def author_may_act(task: Dict[str, Any], client: Any) -> Optional[str]:
    """None when the task's author may act on the task's quest, else the reason to refuse.

    The action runs with the runner owner's Quest key, so Quest's own permission check sees the
    runner, not the person who built the workflow. This is the check that stands in for it. It
    passes when the author owns the quest (``owner_user_ids``), or when Quest stamped
    ``requested_action.authorized`` (server side only, after re-validating the author's edit rights
    at dispatch) for this very author and quest with ``can_edit`` true. The stamp is how co-editors,
    whom a runner key cannot read, get through."""
    quest_id = str(task.get("goal_id") or "")
    if not quest_id:
        return None
    author = str(task.get("created_by") or task.get("user_id") or "")
    if not author:
        return "The task does not say who its author is, so access to the quest cannot be checked."
    stamp = (task.get("requested_action") or {}).get("authorized")
    if (isinstance(stamp, dict) and stamp.get("can_edit") is True
            and str(stamp.get("user_id") or "") == author and str(stamp.get("quest_id") or "") == quest_id):
        return None
    quest = client.get_quest(quest_id) if client is not None else {}
    owners = [str(o) for o in ((quest or {}).get("owner_user_ids") or [])]
    if author in owners:
        return None
    if not owners:
        return "The quest's owners could not be read, so the author's access cannot be checked."
    return "The task's author does not own this quest, so the runner will not act on it for them."


def run_requested_action(task: Dict[str, Any], registry: Any, mcp_servers: Sequence[Any] = (),
                         client: Any = None) -> ActionOutcome:
    request = task.get("requested_action") or {}
    kind, name = request.get("kind"), str(request.get("name") or "")
    if request.get("publisher") != PUBLISHER_ID:
        return ActionOutcome(False, "This action was published by another runner, so this runner will not run it.")
    action = find_published_action(registry, mcp_servers, request)
    if action is None:
        return ActionOutcome(False, f"{name!r} (kind {kind!r}) is not an action this runner publishes.")
    refusal = author_may_act(task, client)
    if refusal:
        return ActionOutcome(False, refusal)
    # Only what the published form asks for is accepted. Anything else (quest_id, task_id, a hidden
    # parameter) comes from the task's own context or not at all.
    allowed = {f["name"] for f in action.get("params_schema") or []}
    args = {k: v for k, v in dict(request.get("args") or {}).items() if k in allowed}
    if kind == KIND_TOOL:
        spec = registry.get(name)
        if any(arg for arg in spec.context_args if arg == "quest_id") and not task.get("goal_id"):
            return ActionOutcome(False, f"{name} needs a quest and this task has none.")
        ctx = ToolContext(quest_id=str(task.get("goal_id") or "") or None,
                          user_id=str(task.get("user_id") or "") or None,
                          task_id=str(task.get("task_id") or task.get("id") or "") or None)
        result = registry.invoke(name, decode_json_params(spec.properties(), args), ctx)
        return ActionOutcome(result.ok, result.text or ("done" if result.ok else f"{name} failed"))
    alias = name.split(":", 1)[0]
    server = next(s for s in mcp_servers if getattr(s, "alias", None) == alias)
    from ..adapters.mcp_write_adapter import MCPWriteAdapter
    written = MCPWriteAdapter(alias=server.alias, spec=server, writable_tools=server.writable_tools).write_operation(name, args)
    if written.ok:
        return ActionOutcome(True, f"Ran {name}.")
    return ActionOutcome(False, written.error or f"{name} failed")
