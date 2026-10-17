"""ACP deep runner, wire vocabulary: environment knobs and protocol constants, the phase names of
QAR's progress vocabulary, tolerant field access over SDK payloads, the translation of one
``session/update`` into an ``EVENT_EXEC`` payload, usage extraction and the stop-reason verdicts.

Pure functions and constants only. ``adapters.acp_deep_runner`` keeps the design notes and
re-exports every name here.
"""
from __future__ import annotations

import logging

from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("quest-ai-runner.acp_deep_runner")


# --- Environment / discovery knobs ------------------------------------------------------------
# Env fallbacks used ONLY when the consumer left the matching AcpConfig field unset — the same
# discipline as core.goal_runner.resolve_deep_timeout_seconds (config wins, env is the floor).
QAR_ACP_NODE_PATH_ENV_VAR = "QAR_ACP_NODE_PATH"
QAR_ACP_AGENT_COMMAND_ENV_VAR = "QAR_ACP_AGENT_COMMAND"

# The npm binary this adapter drives, and the Node major it declares in its ``engines``.
DEFAULT_AGENT_BINARY = "claude-agent-acp"
MIN_NODE_MAJOR = 22

# The ACP protocol version the client speaks, and the steering extension's method name + the
# opt-in that keeps an idle session from silently starting a DETACHED turn (see inject_steering).
ACP_PROTOCOL_VERSION = 1
STEERING_EXT_METHOD = "session/steering"          # sent on the wire as ``_session/steering``
STEERING_IDLE_BEHAVIOR = "promptRequired"

# The session config option ids the Claude ACP agent advertises (agent-side constants). We only
# ever SET one after checking the session actually advertised it, so a future rename degrades to
# "leave the agent on its default" instead of an error.
MODEL_CONFIG_ID = "model"
EFFORT_CONFIG_ID = "effort"

# The Claude model families the agent exposes as ``model`` config values. QAR resolves a full model
# id from its tier config (e.g. ``claude-sonnet-4-5-...``); this maps that id onto the family the
# agent understands. Matching a CONFIG-supplied model id is not the thing hard rule #3 forbids —
# that rule is about gating control flow on words the MODEL generated.
ACP_MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")

# EVENT_EXEC ``phase`` values this runner emits. Deliberately distinct from the terminal phases
# core/guard.py classifies ("done"/"completed"/"failed"/...): a TOOL finishing is not the SUBGOAL
# finishing, and reusing those strings would let one completed Read mark the whole deep task
# succeeded. Only the run's own final tick uses a terminal phase.
PHASE_MESSAGE = "message"
PHASE_THINKING = "thinking"
PHASE_TOOL_CALL = "tool_call"
PHASE_TOOL_PROGRESS = "tool_progress"
PHASE_TOOL_RESULT = "tool_result"
PHASE_TOOL_ERROR = "tool_error"
PHASE_PLAN = "plan"
PHASE_SESSION = "session"
PHASE_STEER = "steer"
PHASE_ESCALATED = "escalated"
PHASE_HEARTBEAT = "heartbeat"
PHASE_DONE = "done"
PHASE_ERROR = "error"

# Display budgets, matched to the ones core/goal_runner.py uses when it renders a Claude Code
# session line, so both runners' live streams read the same.
MAX_MESSAGE_CHARS = 200
MAX_THOUGHT_CHARS = 120


class AcpUnavailable(RuntimeError):
    """The ACP client package or the agent binary could not be used. Caught internally; a caller
    of ``run_goal`` sees a ``DeepResult`` with a clear ``error``, never this exception."""


# ---------------------------------------------------------------------------------------------
# Tolerant field access. The ACP SDK hands us pydantic models with snake_case attributes and
# camelCase aliases; a test double hands us plain dicts or namespaces. Every read of a protocol
# payload goes through these two, so the translation logic is identical for both.
# ---------------------------------------------------------------------------------------------

MISSING = object()


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
            value = getattr(obj, name, MISSING)
            if value is not MISSING:
                return value
        except Exception:  # noqa: BLE001 — a hostile payload must never break translation
            continue
    return default


def meta_of(obj: Any) -> Dict[str, Any]:
    """The ``_meta`` dict of a protocol object (``field_meta`` on the pydantic models), or {}."""
    meta = field_of(obj, "field_meta", "_meta", "meta")
    return meta if isinstance(meta, dict) else {}


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."

def tool_name_of(tool_call: Any) -> Optional[str]:
    """The tool's NAME from the structured protocol payload, or None.

    The Claude ACP agent puts it at ``toolCall._meta.claudeCode.toolName``. We read that field and
    nothing else — never the human-readable ``title``, which the agent composes freely and which
    would make gating depend on wording (hard rule #3's whole point).
    """
    claude_meta = meta_of(tool_call).get("claudeCode")
    if isinstance(claude_meta, dict):
        name = claude_meta.get("toolName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None

# ---------------------------------------------------------------------------------------------
# session/update translation — the ACP stream rendered in QAR's existing progress vocabulary.
# A pure function: given one ACP update, return the EVENT_EXEC payload to emit (or None to drop).
# ---------------------------------------------------------------------------------------------

TOOL_STATUS_PHASES = {
    "pending": PHASE_TOOL_PROGRESS,
    "in_progress": PHASE_TOOL_PROGRESS,
    "completed": PHASE_TOOL_RESULT,
    "failed": PHASE_TOOL_ERROR,
}


def chunk_text(update: Any) -> str:
    content = field_of(update, "content")
    text = field_of(content, "text", default="")
    return text if isinstance(text, str) else ""


def tool_line(update: Any) -> str:
    """A one-line, human-readable label for a tool call, in the same shape the subprocess runner's
    session renderer produces (``$ cmd`` for a shell, ``Tool: path`` for a file op)."""
    title = field_of(update, "title", default="") or ""
    kind = field_of(update, "kind")
    name = tool_name_of(update)
    locations = field_of(update, "locations") or []
    path = None
    if locations:
        path = field_of(locations[0], "path")
    if kind == "execute" and title:
        return f"$ {truncate(title, 80)}"
    if name and path:
        return f"{name}: {path}"
    if name and title:
        return f"{name}: {truncate(title, 80)}"
    if title:
        return f"Using {truncate(title, 80)}"
    return f"Using {name or 'a tool'}"


def translate_session_update(update: Any) -> Optional[Dict[str, Any]]:
    """Map ONE ACP ``session/update`` onto a QAR progress payload, or None to drop it.

    Returns ``{"text": str, "phase": str, "data": {...}}``. The caller wraps it in a
    ``ProgressEvent(type=EVENT_EXEC, ...)`` with the run's own ``run_id`` — the same event type,
    the same ``data["phase"]`` convention, and the same one-line texture a ``claude -p`` deep run
    already streams, so every existing consumer of deep-run progress renders this unchanged.

    Deliberately dropped: ``user_message_chunk`` (our own prompt echoed back), the startup
    ``available_commands_update`` / ``config_option_update`` / ``session_info_update`` chatter, and
    ``usage_update`` (accounted as tokens/cost on the result, not shown as a progress line).
    """
    kind = field_of(update, "session_update", "sessionUpdate")
    if not isinstance(kind, str):
        return None

    if kind == "agent_message_chunk":
        text = chunk_text(update)
        if not text.strip():
            return None
        return {
            "text": truncate(text, MAX_MESSAGE_CHARS),
            "phase": PHASE_MESSAGE,
            "data": {"message_id": field_of(update, "message_id", "messageId")},
        }

    if kind == "agent_thought_chunk":
        text = chunk_text(update)
        if not text.strip():
            return None
        return {"text": f"[thinking] {truncate(text, MAX_THOUGHT_CHARS)}",
                "phase": PHASE_THINKING, "data": {}}

    if kind == "tool_call":
        return {
            "text": tool_line(update),
            "phase": PHASE_TOOL_CALL,
            "data": {
                "tool_call_id": field_of(update, "tool_call_id", "toolCallId"),
                "tool_name": tool_name_of(update),
                "tool_kind": field_of(update, "kind"),
                "status": field_of(update, "status"),
            },
        }

    if kind == "tool_call_update":
        status = field_of(update, "status")
        if not isinstance(status, str):
            return None  # a content-only refresh carries no lifecycle news
        phase = TOOL_STATUS_PHASES.get(status, PHASE_TOOL_PROGRESS)
        title = field_of(update, "title") or field_of(update, "tool_call_id", "toolCallId") or "tool"
        return {
            "text": f"{status}: {truncate(str(title), 80)}",
            "phase": phase,
            "data": {
                "tool_call_id": field_of(update, "tool_call_id", "toolCallId"),
                "tool_name": tool_name_of(update),
                "status": status,
            },
        }

    if kind in ("plan", "plan_update", "plan_removed"):
        entries = field_of(update, "entries") or []
        rendered = [
            {"content": field_of(e, "content"), "status": field_of(e, "status"),
             "priority": field_of(e, "priority")}
            for e in entries
        ]
        done = sum(1 for e in rendered if e.get("status") == "completed")
        return {
            "text": f"Plan: {len(rendered)} task(s), {done} done",
            "phase": PHASE_PLAN,
            "data": {"entries": rendered},
        }

    if kind == "current_mode_update":
        mode = field_of(update, "current_mode_id", "currentModeId")
        if not mode:
            return None
        return {"text": f"Permission mode: {mode}", "phase": PHASE_SESSION, "data": {"mode": mode}}

    return None


def usage_from_update(update: Any) -> Optional[Dict[str, Any]]:
    """Token/cost accounting carried by a ``usage_update``, or None when it is not one."""
    if field_of(update, "session_update", "sessionUpdate") != "usage_update":
        return None
    cost = field_of(update, "cost")
    amount = field_of(cost, "amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    return {"used": field_of(update, "used"), "size": field_of(update, "size"), "cost_usd": amount}


def usage_from_prompt_response(response: Any) -> Tuple[int, float]:
    """``(tokens, cost_usd)`` reported for the whole turn, defaulting to (0, 0.0).

    Mirrors ``core.goal_runner._parse_worker_output``: input+output tokens when the agent breaks
    them out, the reported total otherwise, and 0 when it reports nothing.
    """
    usage = field_of(response, "usage")
    if usage is None:
        return 0, 0.0
    def _int(*names: str) -> int:
        value = field_of(usage, *names)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    tokens = _int("input_tokens", "inputTokens") + _int("output_tokens", "outputTokens")
    if tokens == 0:
        tokens = _int("total_tokens", "totalTokens")
    cost = field_of(usage, "cost")
    amount = field_of(cost, "amount") if cost is not None else None
    try:
        cost_usd = float(amount) if amount is not None else 0.0
    except (TypeError, ValueError):
        cost_usd = 0.0
    return tokens, cost_usd


# The met-vs-not verdict for each ACP stop reason, expressed once. ``end_turn`` is this runner's
# equivalent of the subprocess runner's exit code 0; everything else is a bounded, explainable
# not-met with the reason a human needs.
STOP_REASON_FAILURES = {
    "max_tokens": (
        "The ACP turn stopped because the agent hit its TOKEN limit before finishing. The goal was "
        "not confirmed met; the partial output is below."
    ),
    "max_turn_requests": (
        "The ACP turn stopped because the agent hit its request/turn limit before finishing. The "
        "goal was not confirmed met; the partial output is below."
    ),
    "refusal": (
        "The agent refused to complete this turn. The goal was not met. Read the output for what it "
        "said, and reconsider the brief."
    ),
    "cancelled": (
        "The ACP turn was cancelled before it finished, so the goal was not confirmed met."
    ),
}
