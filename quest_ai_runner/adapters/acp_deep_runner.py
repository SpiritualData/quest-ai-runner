"""ACP deep runner — a SECOND, opt-in ``DeepRunner`` that drives Claude over the Agent Client
Protocol instead of a one-shot ``claude -p`` subprocess.

This is purely ADDITIVE. ``core.goal_runner.SubprocessGoalRunner`` stays exactly as it is and
remains the reference/default worker; a consumer that wants this one wires it explicitly
(``RunnerConfig(deep_runner=AcpDeepRunner(AcpConfig(...)))``). Both satisfy the same
``DeepRunner`` interface, so the brain cannot tell them apart.

WHY IT EXISTS — mid-turn steering
---------------------------------
``SubprocessGoalRunner`` shells out to ``claude -p``: one prompt in, one JSON blob out. Nothing can
reach the worker once it is running, so a user message that arrives mid-run can only be folded in at
the NEXT goal-loop attempt (see the ``_drain_pending`` call in the orchestrator's goal loop). ACP is
a bidirectional JSON-RPC session, and the Claude ACP agent advertises a steering extension
(``InitializeResponse._meta.steering.supported``) whose ``_session/steering`` request INJECTS a
message into the turn that is currently running. That is the one concrete capability this adapter
buys, and it is wired to the SAME mechanism the rest of QAR already uses for mid-run input: an
``InputInbox`` (``core/inbox.py``) drained on a poll while the turn is in flight, or the public
``AcpDeepRunner.steer()`` method any interface can call from its own thread.

WHAT IT TALKS TO
----------------
The agent side is ``@agentclientprotocol/claude-agent-acp`` (npm, Apache-2.0), a Node program that
wraps Anthropic's Claude Agent SDK and speaks ACP over stdio. We spawn it exactly the way
``SubprocessGoalRunner`` spawns ``claude`` — a child process with the consumer's working dir and
environment — but talk a typed protocol to it instead of parsing CLI JSON. It reuses whatever Claude
Code / Agent SDK auth is already active in the environment; there is no extra credential to wire.

  * The Python half is the ``agent-client-protocol`` package (imported as ``acp``), which this repo
    declares as the OPTIONAL ``[acp]`` extra. It is imported LAZILY inside
    ``open_agent_connection`` so importing this module (or ``quest_ai_runner.adapters``) never
    requires it, and a deployment that does not use ACP pays nothing.
  * The Node agent requires **Node >= 22**. The ambient ``node`` on a box is frequently older, so
    the binary used to launch the agent is CONFIG (``AcpConfig.node_path``) with a
    ``QAR_ACP_NODE_PATH`` env fallback, and a too-old Node fails LOUDLY with an actionable message
    rather than dying inside the child process.

Everything here follows this repo's standing contract for a ``DeepRunner``: it NEVER raises. Every
failure — a missing package, a missing binary, an old Node, a protocol error, a wedged turn —
comes back as a ``DeepResult`` with ``met=False`` and a message a human can act on.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from ..core.adapters import (
    EVENT_EXEC,
    DeepResult,
    DeepRunnerBase,
    Escalation,
    ProgressEvent,
)
from ..core.goal_runner import (
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    WEB_TOOLS,
    compose_goal_prompt,
    extract_escalation_id,
    resolve_deep_timeout_seconds,
)

_log = logging.getLogger("quest-ai-runner.acp_deep_runner")


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

_MISSING = object()


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
            value = getattr(obj, name, _MISSING)
            if value is not _MISSING:
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


# ---------------------------------------------------------------------------------------------
# Node / agent-binary resolution. Kept as free functions so a consumer (or a test) can check the
# environment WITHOUT starting a run.
# ---------------------------------------------------------------------------------------------

def resolve_node_binary(configured: Optional[str] = None) -> Optional[str]:
    """The ``node`` used to launch the ACP agent: config, then ``QAR_ACP_NODE_PATH``, then PATH.

    The ambient default on a shared box is routinely an older Node that the agent's ``engines``
    rejects, so this is deliberately overridable per deployment rather than a PATH lookup only.
    """
    for candidate in (configured, os.getenv(QAR_ACP_NODE_PATH_ENV_VAR)):
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        if os.path.sep in candidate:
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        return candidate  # report it as-is; the spawn error names what was missing
    return shutil.which("node")


def node_major_version(node_path: str, *, timeout: float = 10.0) -> Optional[int]:
    """The major version of ``node_path`` (``node --version`` -> ``v22.23.2`` -> 22), or None.

    None means "could not be determined" (binary missing, not executable, unparsable output) —
    the caller reports that honestly rather than assuming the version is fine.
    """
    if not node_path:
        return None
    try:
        proc = subprocess.run([node_path, "--version"], capture_output=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — probing the environment must never raise
        _log.debug("could not probe node version at %s: %s", node_path, e)
        return None
    raw = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    raw = raw.lstrip("vV").split(".")[0].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_agent_entry(configured: Optional[str] = None) -> Optional[str]:
    """The ACP agent program: config, then ``QAR_ACP_AGENT_COMMAND``, then ``claude-agent-acp``
    on PATH. Symlinks are resolved so an npm bin shim resolves to the ``.js`` entry point it
    points at — that is what lets us run it under a CHOSEN node instead of the shebang's."""
    for candidate in (configured, os.getenv(QAR_ACP_AGENT_COMMAND_ENV_VAR)):
        candidate = (candidate or "").strip()
        if candidate:
            return candidate
    return shutil.which(DEFAULT_AGENT_BINARY)


def build_agent_argv(
    *,
    agent_command: Optional[str],
    agent_args: Optional[List[str]],
    node_path: Optional[str],
    min_node_major: int = MIN_NODE_MAJOR,
) -> Tuple[List[str], Optional[str]]:
    """Resolve the argv that launches the ACP agent, or an explanatory error.

    Returns ``(argv, None)`` on success and ``([], "<message>")`` on failure. Never raises. The
    message is written for the human reading a failed task: it names what was looked for, what was
    found, and which knob fixes it.

    A JavaScript entry point is launched as ``[<node>, <entry>, *args]`` rather than executed
    directly, so the Node the consumer chose is the one that runs — an npm shim's shebang would
    otherwise pick up whatever ``node`` happens to be first on PATH (frequently a too-old one).
    """
    entry = resolve_agent_entry(agent_command)
    if not entry:
        return [], (
            f"ACP agent program not found: no {DEFAULT_AGENT_BINARY!r} on PATH. Install it "
            f"(npm i -g @agentclientprotocol/claude-agent-acp) or set AcpConfig.agent_command / "
            f"{QAR_ACP_AGENT_COMMAND_ENV_VAR} to its path."
        )

    resolved_entry = entry
    try:
        path = Path(entry)
        if path.exists():
            resolved_entry = str(path.resolve())
    except Exception:  # noqa: BLE001 — a weird path just stays as given
        resolved_entry = entry

    extra = list(agent_args or [])
    if not resolved_entry.endswith(".js"):
        # Not a JS entry point — treat it as a self-contained executable the consumer trusts and
        # run it directly. Node is then that program's problem, not ours.
        return [resolved_entry, *extra], None

    node = resolve_node_binary(node_path)
    if not node:
        return [], (
            f"ACP agent needs Node >= {min_node_major} to run {resolved_entry}, but no node binary "
            f"was found. Install Node {min_node_major}+ and set AcpConfig.node_path or "
            f"{QAR_ACP_NODE_PATH_ENV_VAR} to it."
        )
    major = node_major_version(node)
    if major is None:
        return [], (
            f"ACP agent needs Node >= {min_node_major}, but the node at {node!r} could not be run "
            f"(``node --version`` failed). Set AcpConfig.node_path or {QAR_ACP_NODE_PATH_ENV_VAR} "
            f"to a working Node {min_node_major}+ binary."
        )
    if major < min_node_major:
        return [], (
            f"ACP agent requires Node >= {min_node_major}, found v{major} at {node!r}. A distro's "
            f"packaged Node is often too old; install a newer one (nvm, a vendor build, a container "
            f"image, whatever this deployment uses) and point AcpConfig.node_path or "
            f"{QAR_ACP_NODE_PATH_ENV_VAR} at it. The system default is left untouched."
        )
    return [node, resolved_entry, *extra], None


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------

@dataclass
class AcpConfig:
    """Config for the ACP deep runner — all consumer-supplied, nothing baked in.

    Mirrors ``core.goal_runner.SubprocessConfig`` field-for-field where the two runners mean the
    same thing (``working_dir``, ``context_preamble``, ``skip_permissions``, the tool gating,
    ``timeout_seconds``, ``extra_path_dirs``), so switching a consumer between them is a one-line
    change. The ACP-only fields are the agent/Node resolution and the steering wiring.
    """

    working_dir: str                                  # where the agent session runs (corpus root)

    # --- how the agent process is launched -----------------------------------------------
    agent_command: Optional[str] = None               # path to claude-agent-acp / its .js entry
    agent_args: List[str] = field(default_factory=list)
    node_path: Optional[str] = None                   # the node that runs a .js entry point
    min_node_major: int = MIN_NODE_MAJOR
    extra_path_dirs: Optional[List[str]] = None       # prepended to the child's PATH

    # --- what the run is told and allowed to do -------------------------------------------
    context_preamble: str = ""                        # org/persona context prepended to the brief
    skip_permissions: bool = True                     # auto-approve tool permission requests
    allowed_tools: Optional[List[str]] = None         # pin the tool set (None = agent default)
    disallowed_tools: Optional[List[str]] = None      # deny specific tools
    permission_mode: Optional[str] = None             # an agent session mode id, e.g. "bypassPermissions"
    effort: Optional[str] = None                      # agent effort level, e.g. "high"
    timeout_seconds: Optional[float] = None           # wall-clock cap; None -> the shared floor
    # MCP servers made available to the agent's session (the ACP ``session/new`` ``mcpServers``
    # param), in whatever shape the ACP agent's own wire format expects (a list of server config
    # dicts). Empty by default -- was previously hardcoded to ``[]`` in ``_drive_turn`` with no way
    # for a consumer to configure it; passing this through is purely additive.
    mcp_servers: List[Dict[str, Any]] = field(default_factory=list)

    # --- the human fork -------------------------------------------------------------------
    # An EscalationSink. Used ONLY when skip_permissions is False and the agent asks for a
    # permission this config does not auto-decide: the ask becomes a real decision-request and the
    # run comes back as DeepResult(met=False, decision_id=...), i.e. the SAME "needs_you" contract
    # the QAR-ESCALATED marker gives the subprocess runner — no parallel permission system.
    escalation: Optional[Any] = None
    escalation_assignee: Optional[str] = None
    escalation_quest_id: Optional[str] = None

    # --- mid-turn steering ----------------------------------------------------------------
    # Wire EITHER an InputInbox + the conversation id it is keyed by (the normal case: QAR's own
    # core/inbox.py, the same inbox the orchestrator drains between goal-loop attempts), OR the two
    # raw callables for a consumer whose queue lives somewhere else. ``steering_return`` is where a
    # message goes back when it could NOT be injected (the turn had already settled), so nothing is
    # silently swallowed; the inbox form supplies it automatically.
    steering_inbox: Optional[Any] = None
    steering_conversation_id: Optional[str] = None
    steering_source: Optional[Callable[[], List[str]]] = None
    steering_return: Optional[Callable[[str], None]] = None
    steering_poll_seconds: float = 1.0

    heartbeat_seconds: float = DEFAULT_MONITOR_HEARTBEAT_SECONDS
    max_output_chars: int = 200_000                   # cap on accumulated agent text per run

    def web_enabled(self) -> bool:
        """Whether the spawned agent can BROWSE the live web (WebSearch/WebFetch reachable).

        Same honest derivation as ``SubprocessConfig.web_enabled`` — ``config.derive_capabilities``
        reads this off ``deep_runner.cfg`` generically, so this runner reports its capabilities
        through exactly the same path.
        """
        disallowed = {t.strip() for t in (self.disallowed_tools or [])}
        if any(t in disallowed for t in WEB_TOOLS):
            return False
        if self.allowed_tools is not None:
            allowed = {t.strip() for t in self.allowed_tools}
            return any(t in allowed for t in WEB_TOOLS)
        return True


# ---------------------------------------------------------------------------------------------
# Permission mapping — pure functions, so the policy is testable without a protocol at all.
# ---------------------------------------------------------------------------------------------

ALLOW = "allow"
REJECT = "reject"
HUMAN = "human"


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


def classify_permission(cfg: AcpConfig, tool_name: Optional[str]) -> Tuple[str, str]:
    """Decide a permission ask from the SAME config surface the subprocess runner uses.

    Returns ``(ALLOW | REJECT | HUMAN, reason)``. The order matters: an explicit denial beats an
    auto-approval, so ``skip_permissions=True`` never overrides a tool the consumer disallowed.

    Fails CLOSED on an unidentifiable tool when the consumer pinned ``allowed_tools``: if we cannot
    tell what is being asked for, we cannot tell that it is on the allowed list.
    """
    disallowed = {t.strip() for t in (cfg.disallowed_tools or [])}
    if tool_name and tool_name in disallowed:
        return REJECT, f"{tool_name} is in disallowed_tools"
    if cfg.allowed_tools is not None:
        allowed = {t.strip() for t in cfg.allowed_tools}
        if tool_name is None:
            return REJECT, "allowed_tools is pinned and the requested tool could not be identified"
        if tool_name not in allowed:
            return REJECT, f"{tool_name} is not in allowed_tools"
    if cfg.skip_permissions:
        return ALLOW, "skip_permissions is on (autonomous run)"
    return HUMAN, "skip_permissions is off, so a human decides this"


def select_option(options: Any, kinds: Tuple[str, ...]) -> Optional[str]:
    """The ``optionId`` of the first offered option whose ``kind`` matches, in preference order.

    Chosen by the structured ``kind`` field only (``allow_once`` / ``allow_always`` /
    ``reject_once`` / ``reject_always``), never by the option's display ``name``.
    """
    for wanted in kinds:
        for option in list(options or []):
            if field_of(option, "kind") == wanted:
                option_id = field_of(option, "option_id", "optionId")
                if isinstance(option_id, str) and option_id:
                    return option_id
    return None


def permission_response(option_id: Optional[str]) -> Dict[str, Any]:
    """The ACP ``RequestPermissionResponse`` payload: a selected option, or a cancellation.

    Returned as a plain wire-shaped dict (camelCase) so this module needs no SDK import to answer.
    """
    if option_id:
        return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------------------------------------
# session/update translation — the ACP stream rendered in QAR's existing progress vocabulary.
# A pure function: given one ACP update, return the EVENT_EXEC payload to emit (or None to drop).
# ---------------------------------------------------------------------------------------------

_TOOL_STATUS_PHASES = {
    "pending": PHASE_TOOL_PROGRESS,
    "in_progress": PHASE_TOOL_PROGRESS,
    "completed": PHASE_TOOL_RESULT,
    "failed": PHASE_TOOL_ERROR,
}


def _chunk_text(update: Any) -> str:
    content = field_of(update, "content")
    text = field_of(content, "text", default="")
    return text if isinstance(text, str) else ""


def _tool_line(update: Any) -> str:
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
        text = _chunk_text(update)
        if not text.strip():
            return None
        return {
            "text": truncate(text, MAX_MESSAGE_CHARS),
            "phase": PHASE_MESSAGE,
            "data": {"message_id": field_of(update, "message_id", "messageId")},
        }

    if kind == "agent_thought_chunk":
        text = _chunk_text(update)
        if not text.strip():
            return None
        return {"text": f"[thinking] {truncate(text, MAX_THOUGHT_CHARS)}",
                "phase": PHASE_THINKING, "data": {}}

    if kind == "tool_call":
        return {
            "text": _tool_line(update),
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
        phase = _TOOL_STATUS_PHASES.get(status, PHASE_TOOL_PROGRESS)
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


# ---------------------------------------------------------------------------------------------
# The live connection seam. Tests replace THIS symbol; nothing else in the module touches the SDK.
# ---------------------------------------------------------------------------------------------

@asynccontextmanager
async def open_agent_connection(
    client: Any,
    argv: List[str],
    *,
    env: Dict[str, str],
    cwd: str,
) -> AsyncIterator[Tuple[Any, Any]]:
    """Spawn the ACP agent and yield ``(connection, process)``.

    The ONE place the optional ``agent-client-protocol`` package is imported, and the one place a
    subprocess is created — so an offline test replaces this single module-level name with its own
    async context manager and exercises the entire adapter without a protocol, a process, or auth.
    """
    try:
        from acp import spawn_agent_process  # noqa: PLC0415 — optional [acp] extra, imported lazily
    except ImportError as e:  # pragma: no cover — exercised via the error path, not the import
        raise AcpUnavailable(
            "the ACP client package is not installed. Install the optional extra: "
            "pip install 'quest-ai-runner[acp]' (provides agent-client-protocol)."
        ) from e
    async with spawn_agent_process(client, argv[0], *argv[1:], env=env, cwd=cwd) as (conn, process):
        yield conn, process


# ---------------------------------------------------------------------------------------------
# One live run: the shared state between the async driver and the ACP client callbacks.
# ---------------------------------------------------------------------------------------------

class AcpRun:
    """State for ONE deep run: its session, its accumulated output, and its steering channel."""

    def __init__(self, *, cfg: AcpConfig, goal: str, run_id: Optional[str],
                 emit: Optional[Callable[[ProgressEvent], None]]):
        self.cfg = cfg
        self.goal = goal
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.key = uuid.uuid4().hex           # registry key: unique even if two runs share a run_id
        self.emit = emit
        self.conn: Any = None
        self.session_id: Optional[str] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.steering_supported = False
        # Latches OFF the first time an injection does not land (the turn had already settled, or
        # the wire errored). Without it, a returned message would be drained and re-offered on the
        # very next poll tick, forever. Once closed, pending messages simply stay in the queue for
        # the orchestrator's own between-attempts drain.
        self.steering_open = True
        self.decision_id: Optional[str] = None
        self.permission_notes: List[str] = []
        self.injected: List[str] = []
        self.tokens = 0
        self.cost_usd = 0.0
        self.started = time.time()
        self.last_activity = self.started
        # Agent text, bucketed by message id in arrival order. The FINAL bucket is the turn's
        # result (the same thing ``claude -p --output-format json`` reports as ``result``);
        # everything before it is narration the live stream already showed.
        self.messages: Dict[str, List[str]] = {}

    # --- progress ------------------------------------------------------------------------
    def emit_exec(self, text: str, phase: str, **data: Any) -> None:
        """Emit one EVENT_EXEC tick. Never raises: a broken sink must not break the run."""
        if self.emit is None:
            return
        payload: Dict[str, Any] = {"run_id": self.run_id, "phase": phase}
        payload.update({k: v for k, v in data.items() if v is not None})
        try:
            self.emit(ProgressEvent(type=EVENT_EXEC, text=text, data=payload))
        except Exception:  # noqa: BLE001
            _log.debug("acp progress sink raised; continuing", exc_info=True)

    def note_activity(self) -> None:
        self.last_activity = time.time()

    # --- output --------------------------------------------------------------------------
    def add_agent_text(self, message_id: Optional[str], text: str) -> None:
        bucket = self.messages.setdefault(str(message_id or ""), [])
        if sum(len(chunk) for chunk in bucket) < self.cfg.max_output_chars:
            bucket.append(text)

    def final_output(self) -> str:
        """The turn's result text: the LAST agent message, falling back to everything.

        Matching ``claude -p``'s ``result`` semantics keeps the goal verifier reading the same kind
        of payload from both runners. The fallback covers an agent that never sets ``messageId``
        (then there is exactly one bucket, which IS everything).
        """
        if not self.messages:
            return ""
        buckets = list(self.messages.values())
        last = "".join(buckets[-1]).strip()
        if last:
            return last[: self.cfg.max_output_chars]
        joined = "".join(chunk for bucket in buckets for chunk in bucket).strip()
        return joined[: self.cfg.max_output_chars]

    # --- steering ------------------------------------------------------------------------
    def drain_steering(self) -> List[str]:
        """Pending mid-run messages for this run, from the inbox or the raw source. Never raises."""
        cfg = self.cfg
        try:
            if cfg.steering_inbox is not None and cfg.steering_conversation_id:
                return [m for m in cfg.steering_inbox.drain(cfg.steering_conversation_id) if m]
            if cfg.steering_source is not None:
                return [str(m) for m in (cfg.steering_source() or []) if str(m).strip()]
        except Exception:  # noqa: BLE001 — a broken queue must never break the run
            _log.debug("acp steering drain failed; continuing", exc_info=True)
        return []

    def return_steering(self, message: str) -> None:
        """Hand a message that could NOT be injected back to where it came from, so it is folded
        into the next attempt by the orchestrator's own drain instead of being lost."""
        cfg = self.cfg
        try:
            if cfg.steering_inbox is not None and cfg.steering_conversation_id:
                cfg.steering_inbox.push(cfg.steering_conversation_id, message)
                return
            if cfg.steering_return is not None:
                cfg.steering_return(message)
                return
        except Exception:  # noqa: BLE001
            _log.debug("acp steering return failed; message not requeued", exc_info=True)
        _log.warning("acp steering: message could not be injected and had nowhere to go back to")

    async def inject_steering(self, message: str) -> bool:
        """Inject ``message`` into the turn CURRENTLY RUNNING via ``_session/steering``.

        Returns True when the agent confirms the injection. ``idleBehavior=promptRequired`` is sent
        deliberately: without it, an agent whose turn already settled starts a NEW detached turn,
        which would run unbounded work outside the orchestrator's goal loop and outside this run's
        timeout. With it, an already-settled turn answers ``promptRequired`` and the message goes
        back to the queue for the next attempt.
        """
        if not message or not message.strip():
            return False
        if self.conn is None or not self.session_id or not self.steering_supported \
                or not self.steering_open:
            self.return_steering(message)
            return False
        params = {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": message}],
            "_meta": {"steering": {"idleBehavior": STEERING_IDLE_BEHAVIOR}},
        }
        try:
            result = await self.conn.ext_method(STEERING_EXT_METHOD, params)
        except Exception as e:  # noqa: BLE001 — a wire error must not end the run
            _log.warning("acp steering request failed: %s", e)
            self.steering_open = False
            self.return_steering(message)
            return False
        outcome = field_of(result, "outcome")
        if outcome in ("injected", "startedNewTurn"):
            self.injected.append(message)
            self.note_activity()
            self.emit_exec(f"Steering the running turn: {truncate(message, MAX_MESSAGE_CHARS)}",
                           PHASE_STEER, outcome=outcome)
            return True
        # ``promptRequired`` (or anything unexpected) means there is no running turn to steer:
        # close the channel so the returned message waits for the next attempt instead of being
        # re-offered on every poll tick.
        self.steering_open = False
        self.return_steering(message)
        _log.info("acp steering not injected (outcome=%r); message returned to the queue", outcome)
        return False

    # --- permissions ---------------------------------------------------------------------
    async def decide_permission(self, tool_call: Any, options: Any) -> Dict[str, Any]:
        """Answer one ``session/request_permission`` using QAR's existing permission model."""
        tool_name = tool_name_of(tool_call)
        verdict, reason = classify_permission(self.cfg, tool_name)

        if verdict == HUMAN:
            decision_id = await self._escalate_permission(tool_call, tool_name, reason)
            if decision_id:
                self.decision_id = decision_id
                self.emit_exec(
                    f"Paused for a human decision on {tool_name or 'a tool'} (decision {decision_id})",
                    PHASE_ESCALATED, tool_name=tool_name, decision_id=decision_id,
                )
                # Deny the tool so the turn stops here rather than continuing without the approval.
                # The run comes back as needs_you with the decision linked.
                return permission_response(select_option(options, ("reject_once", "reject_always")))
            verdict = REJECT
            reason = (f"{reason}, but no escalation sink is wired, so nobody could be asked")

        if verdict == ALLOW:
            option_id = select_option(options, ("allow_always", "allow_once"))
            self.emit_exec(f"Allowed {tool_name or 'a tool'} ({reason})",
                           PHASE_TOOL_PROGRESS, tool_name=tool_name, permission="allow")
            return permission_response(option_id)

        self.permission_notes.append(f"denied {tool_name or 'an unidentified tool'}: {reason}")
        self.emit_exec(f"Denied {tool_name or 'a tool'} ({reason})",
                       PHASE_TOOL_PROGRESS, tool_name=tool_name, permission="reject")
        return permission_response(select_option(options, ("reject_once", "reject_always")))

    async def _escalate_permission(self, tool_call: Any, tool_name: Optional[str],
                                   reason: str) -> Optional[str]:
        sink = self.cfg.escalation
        if sink is None:
            return None
        title = field_of(tool_call, "title") or tool_name or "a tool"
        escalation = Escalation(
            summary=(f"A deep run needs approval to use {tool_name or 'a tool'} "
                     f"({truncate(str(title), 160)}) while working on: {truncate(self.goal, 200)}"),
            kind="approve",
            quest_id=self.cfg.escalation_quest_id,
            assignee=self.cfg.escalation_assignee,
            default_on_silence="hold",
        )
        try:
            # The sink is a synchronous consumer call (an HTTP POST in production); keep it off the
            # protocol event loop so a slow decision service cannot stall the session's I/O.
            return await asyncio.to_thread(sink.escalate, escalation)
        except Exception as e:  # noqa: BLE001 — a failed escalation degrades to a denial
            _log.warning("acp permission escalation failed (%s); denying the tool instead", e)
            return None


class AcpSessionClient:
    """The CLIENT half of the ACP connection: what the agent calls back into.

    A plain class, deliberately: the SDK's ``Client`` is a ``typing.Protocol``, so this satisfies it
    structurally with no import and no inheritance, which is what lets the module load (and the
    tests run) without the optional package installed.

    ``write_text_file`` / ``read_text_file`` / the terminal methods are intentionally ABSENT: we
    advertise ``fs: {readTextFile: false, writeTextFile: false}`` and no terminal capability, and an
    absent handler answers "method not found" — the honest response for a capability we declined.
    The agent does its own file and shell work in its own process.
    """

    def __init__(self, run: AcpRun):
        self.run = run

    def on_connect(self, conn: Any) -> None:
        self.run.conn = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        run = self.run
        try:
            run.note_activity()
            usage = usage_from_update(update)
            if usage is not None:
                if usage.get("cost_usd"):
                    run.cost_usd = float(usage["cost_usd"])
                return
            if field_of(update, "session_update", "sessionUpdate") == "agent_message_chunk":
                run.add_agent_text(field_of(update, "message_id", "messageId"), _chunk_text(update))
            payload = translate_session_update(update)
            if payload:
                run.emit_exec(payload["text"], payload["phase"], **(payload.get("data") or {}))
        except Exception:  # noqa: BLE001 — a translation bug must not kill the session
            _log.debug("acp session_update handling failed; continuing", exc_info=True)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kwargs: Any) -> Dict[str, Any]:
        try:
            return await self.run.decide_permission(tool_call, options)
        except Exception:  # noqa: BLE001 — never answer a permission ask with an exception
            _log.warning("acp permission handling failed; cancelling the ask", exc_info=True)
            return permission_response(None)

    async def ext_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Agent-initiated extension requests. We implement none; answer benignly rather than
        erroring, so an agent probing an extension never fails the run."""
        _log.debug("acp: ignoring agent extension request %r", method)
        return {}

    async def ext_notification(self, method: str, params: Dict[str, Any]) -> None:
        _log.debug("acp: ignoring agent extension notification %r", method)


# ---------------------------------------------------------------------------------------------
# The DeepRunner
# ---------------------------------------------------------------------------------------------

class AcpDeepRunner(DeepRunnerBase):
    """A ``DeepRunner`` that runs each goal as one ACP session against the Claude ACP agent.

    SESSION LIFECYCLE — one process + one session per ``run_goal`` call, torn down when it returns.
    That is deliberately the SAME lifetime ``SubprocessGoalRunner`` gives a ``claude -p`` spawn: the
    orchestrator's goal loop calls ``run_goal`` once per attempt and never signals "this subgoal is
    finished", so a session held across calls would have no defined moment to close and would leak a
    Node process per retry. Continuity across attempts is already handled a level up, by the loop
    feeding each retry a brief refined with what fell short.

    STEERING — while the turn is in flight, a background task polls the configured inbox/source
    every ``steering_poll_seconds`` and injects anything it finds into the RUNNING turn. Any thread
    can also call ``steer()`` directly. Both routes go through ``_session/steering``; a message that
    arrives after the turn settled is handed back to the queue rather than dropped.
    """

    def __init__(self, config: AcpConfig):
        self.cfg = config
        self._runs: Dict[str, AcpRun] = {}
        self._runs_lock = threading.Lock()

    # --- public steering surface ---------------------------------------------------------
    def active_runs(self) -> List[str]:
        """The ``run_id``s with a live ACP session right now."""
        with self._runs_lock:
            return [run.run_id for run in self._runs.values()]

    def steer(self, message: str, *, run_id: Optional[str] = None, timeout: float = 10.0) -> bool:
        """Inject ``message`` into a RUNNING deep turn from any thread. Returns True if injected.

        ``run_id`` selects one live run; omitted, the message goes to every live run (the common
        case is exactly one). This is the direct route for an interface that already holds the
        message; the inbox wiring in ``AcpConfig`` is the hands-off route for one that does not.
        """
        with self._runs_lock:
            targets = [r for r in self._runs.values() if run_id is None or r.run_id == run_id]
        injected = False
        for run in targets:
            loop = run.loop
            if loop is None or loop.is_closed():
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(run.inject_steering(message), loop)
                injected = bool(future.result(timeout=timeout)) or injected
            except Exception as e:  # noqa: BLE001 — steering is best-effort, never fatal
                _log.warning("acp steer() failed for run %s: %s", run.run_id, e)
        return injected

    # --- DeepRunner ----------------------------------------------------------------------
    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None,
                 working_dir: Optional[str] = None) -> DeepResult:
        """Run ``goal`` as one ACP turn. Signature matches ``SubprocessGoalRunner.run_goal`` exactly.

        ``max_turns`` is accepted for interface parity but is NOT enforceable over ACP today: the
        protocol has no turn budget and the Claude ACP agent exposes none. The run is bounded by the
        wall-clock timeout here and by the orchestrator's own goal loop (attempts + token budget),
        which is where the real bound lives for both runners anyway.
        """
        preamble = self.cfg.context_preamble if context_preamble is None else context_preamble
        prompt = compose_goal_prompt(goal, brief, preamble=preamble)
        effective_working_dir = self.cfg.working_dir if working_dir is None else working_dir
        timeout = resolve_deep_timeout_seconds(self.cfg.timeout_seconds)

        argv, argv_error = build_agent_argv(
            agent_command=self.cfg.agent_command,
            agent_args=self.cfg.agent_args,
            node_path=self.cfg.node_path,
            min_node_major=self.cfg.min_node_major,
        )
        if argv_error:
            return DeepResult(met=False, error=argv_error)

        run = AcpRun(cfg=self.cfg, goal=goal, run_id=run_id, emit=emit)
        box: Dict[str, Any] = {}

        def drive() -> None:
            try:
                box["result"] = asyncio.run(
                    self._run_session(run, argv=argv, prompt=prompt, model=model,
                                      working_dir=effective_working_dir, timeout=timeout)
                )
            except BaseException as e:  # noqa: BLE001 — carried out, never raised at the caller
                box["error"] = e

        # Always drive the session on its own thread with its own event loop: ``run_goal`` is a
        # synchronous contract that may be called from anywhere (including a thread that already
        # has a loop), and the thread's loop is also what ``steer()`` targets cross-thread.
        thread = threading.Thread(target=drive, name=f"qar-acp-{run.run_id}", daemon=True)
        thread.start()
        thread.join(timeout=timeout + 60.0)
        if thread.is_alive():
            self._unregister(run)
            return DeepResult(
                met=False,
                error=(f"The ACP deep run did not return within {timeout + 60:.0f}s (its own "
                       f"{timeout:.0f}s turn timeout should have fired first). Treating it as a "
                       "hard failure; the agent process is torn down when its thread ends."),
            )
        if "error" in box:
            e = box["error"]
            _log.error("acp deep run failed: %s: %s", type(e).__name__, e, exc_info=True)
            return DeepResult(met=False, error=f"ACP deep run failed: {type(e).__name__}: {e}")
        result = box.get("result")
        if not isinstance(result, DeepResult):
            return DeepResult(met=False, error="ACP deep run produced no result")
        return result

    # --- internals -----------------------------------------------------------------------
    def _register(self, run: AcpRun) -> None:
        with self._runs_lock:
            self._runs[run.key] = run

    def _unregister(self, run: AcpRun) -> None:
        with self._runs_lock:
            self._runs.pop(run.key, None)

    def build_env(self) -> Dict[str, str]:
        """The child's environment. Mirrors ``SubprocessGoalRunner._build_env``: the spawned agent
        must not inherit our own Claude Code session or API billing, so those keys are dropped and
        it authenticates the same way the reference deep runner's worker does."""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        path_dirs = list(self.cfg.extra_path_dirs or [])
        node = resolve_node_binary(self.cfg.node_path)
        if node and os.path.sep in node:
            # Put the CHOSEN node's directory first so anything the agent shells out to (its own
            # child tooling, npx) resolves to the same Node the engine check passed.
            path_dirs.insert(0, str(Path(node).parent))
        if path_dirs:
            current = env.get("PATH", "")
            for directory in path_dirs:
                if directory and directory not in current:
                    current = f"{directory}:{current}"
            env["PATH"] = current
        return env

    async def _run_session(self, run: AcpRun, *, argv: List[str], prompt: str,
                           model: Optional[str], working_dir: str, timeout: float) -> DeepResult:
        run.loop = asyncio.get_running_loop()
        client = AcpSessionClient(run)
        try:
            async with open_agent_connection(client, argv, env=self.build_env(), cwd=working_dir) as (conn, _proc):
                run.conn = conn
                return await self._drive_turn(run, conn, prompt=prompt, model=model,
                                              working_dir=working_dir, timeout=timeout)
        except AcpUnavailable as e:
            return DeepResult(met=False, error=f"ACP deep run could not start: {e}")
        except FileNotFoundError:
            return DeepResult(met=False, error=f"ACP agent program not found: {argv[0]!r}")
        except PermissionError as e:
            return DeepResult(met=False, error=f"permission denied running the ACP agent: {e}")
        except Exception as e:  # noqa: BLE001 — every wire failure is a reported result
            _log.error("acp session failed: %s: %s", type(e).__name__, e, exc_info=True)
            return DeepResult(met=False,
                              error=f"ACP session failed: {type(e).__name__}: {e}")
        finally:
            self._unregister(run)
            run.conn = None

    async def _drive_turn(self, run: AcpRun, conn: Any, *, prompt: str, model: Optional[str],
                          working_dir: str, timeout: float) -> DeepResult:
        init = await conn.initialize(
            protocol_version=ACP_PROTOCOL_VERSION,
            client_capabilities={"fs": {"readTextFile": False, "writeTextFile": False}},
        )
        steering = meta_of(init).get("steering")
        run.steering_supported = bool(isinstance(steering, dict) and steering.get("supported"))
        if not run.steering_supported:
            _log.info("acp agent does not advertise steering; mid-run injection is unavailable")

        session = await conn.new_session(cwd=working_dir, mcp_servers=self.cfg.mcp_servers)
        run.session_id = field_of(session, "session_id", "sessionId")
        if not run.session_id:
            return DeepResult(met=False,
                              error="the ACP agent created no session (session/new returned no id)")
        self._register(run)
        run.emit_exec(f"ACP session started in {working_dir}", PHASE_SESSION,
                      steering=run.steering_supported)

        await self._apply_session_config(run, conn, session, model)

        steward = asyncio.create_task(self._steering_and_heartbeat(run))
        try:
            response = await asyncio.wait_for(
                conn.prompt(session_id=run.session_id,
                            prompt=[{"type": "text", "text": prompt}]),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.time() - run.started
            await self._safe_cancel(conn, run.session_id)
            run.emit_exec("Deep run exceeded its wall-clock timeout", PHASE_ERROR)
            return DeepResult(
                met=False, output=run.final_output(), tokens=run.tokens, cost_usd=run.cost_usd,
                error=(f"Deep run exceeded its wall-clock timeout: ran for {elapsed:.0f}s against a "
                       f"{timeout:.0f}s limit. The ACP turn was cancelled. This is a hard failure, "
                       "not a silent success, even if partial output exists."),
            )
        finally:
            steward.cancel()
            try:
                await steward
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown never raises
                pass
            await self._safe_close(conn, run.session_id)

        return self._result_from_response(run, response)

    async def _apply_session_config(self, run: AcpRun, conn: Any, session: Any,
                                    model: Optional[str]) -> None:
        """Set the model / effort / permission mode — but only options the session ADVERTISED.

        Checking against the agent's own advertised surface (rather than firing the requests
        hopefully) means an agent that renames or drops an option leaves us on its default instead
        of failing the run.
        """
        options = field_of(session, "config_options", "configOptions") or []
        advertised: Dict[str, List[str]] = {}
        for option in options:
            option_id = field_of(option, "id")
            values = [field_of(v, "value") for v in (field_of(option, "options") or [])]
            if isinstance(option_id, str):
                advertised[option_id] = [v for v in values if isinstance(v, str)]

        family = acp_model_family(model)
        if family and family in advertised.get(MODEL_CONFIG_ID, []):
            await self._safe_set_option(conn, run, MODEL_CONFIG_ID, family)
        elif model and family is None:
            _log.debug("acp: %r is not a Claude family the agent offers; using its default", model)

        if self.cfg.effort and self.cfg.effort in advertised.get(EFFORT_CONFIG_ID, []):
            await self._safe_set_option(conn, run, EFFORT_CONFIG_ID, self.cfg.effort)

        if self.cfg.permission_mode:
            modes = field_of(session, "modes")
            available = [field_of(m, "id") for m in (field_of(modes, "available_modes", "availableModes") or [])]
            if self.cfg.permission_mode in available:
                try:
                    await conn.set_session_mode(session_id=run.session_id,
                                                mode_id=self.cfg.permission_mode)
                except Exception as e:  # noqa: BLE001 — an unsupported mode is not fatal
                    _log.debug("acp: could not set session mode %r: %s", self.cfg.permission_mode, e)
            else:
                _log.debug("acp: session does not offer mode %r; leaving the agent's default",
                           self.cfg.permission_mode)

    async def _safe_set_option(self, conn: Any, run: AcpRun, config_id: str, value: str) -> None:
        try:
            await conn.set_config_option(config_id=config_id, session_id=run.session_id, value=value)
        except Exception as e:  # noqa: BLE001 — a rejected option leaves the agent's default
            _log.debug("acp: could not set config option %s=%s: %s", config_id, value, e)

    async def _safe_cancel(self, conn: Any, session_id: Optional[str]) -> None:
        if not session_id:
            return
        try:
            await conn.cancel(session_id=session_id)
        except Exception as e:  # noqa: BLE001
            _log.debug("acp: cancel failed: %s", e)

    async def _safe_close(self, conn: Any, session_id: Optional[str]) -> None:
        if not session_id:
            return
        close = getattr(conn, "close_session", None)
        if close is None:
            return
        try:
            await close(session_id=session_id)
        except Exception as e:  # noqa: BLE001 — the process teardown closes it either way
            _log.debug("acp: close_session failed: %s", e)

    async def _steering_and_heartbeat(self, run: AcpRun) -> None:
        """While the turn runs: inject queued messages, and keep the stream audibly alive.

        The heartbeat carries the same rule as the subprocess runner's session monitor — silence
        longer than about 10s while a deep run is working is a bug by definition — so a wedged ACP
        turn looks different from a working one.
        """
        interval = max(0.05, float(run.cfg.steering_poll_seconds or 1.0))
        heartbeat = max(1.0, float(run.cfg.heartbeat_seconds or DEFAULT_MONITOR_HEARTBEAT_SECONDS))
        last_beat = time.time()
        while True:
            await asyncio.sleep(interval)
            if run.steering_supported and run.steering_open:
                for message in run.drain_steering():
                    await run.inject_steering(message)
            now = time.time()
            if now - run.last_activity >= heartbeat and now - last_beat >= heartbeat:
                last_beat = now
                elapsed = now - run.started
                run.emit_exec(f"Deep run active, {elapsed:.0f}s elapsed, waiting on the agent",
                              PHASE_HEARTBEAT, elapsed_seconds=round(elapsed, 1))

    def _result_from_response(self, run: AcpRun, response: Any) -> DeepResult:
        """Turn the settled ACP turn into a ``DeepResult``, in the same shape both runners return."""
        output = run.final_output()
        tokens, cost = usage_from_prompt_response(response)
        run.tokens = tokens or run.tokens
        run.cost_usd = cost or run.cost_usd
        stop_reason = field_of(response, "stop_reason", "stopReason")

        # A human decision raised mid-run — either structurally (a permission ask we escalated) or
        # by the worker printing the marker. Both mean PAUSED-ON-A-HUMAN, which outranks the stop
        # reason: the executor must report needs_you with the decision linked.
        decision_id = run.decision_id or extract_escalation_id(output)
        if decision_id:
            run.emit_exec("Deep run paused on a human decision", PHASE_ERROR)
            return DeepResult(met=False, output=output, decision_id=decision_id,
                              tokens=run.tokens, cost_usd=run.cost_usd)

        if stop_reason in STOP_REASON_FAILURES:
            run.emit_exec(f"Deep run stopped: {stop_reason}", PHASE_ERROR)
            return DeepResult(met=False, output=output, tokens=run.tokens, cost_usd=run.cost_usd,
                              error=STOP_REASON_FAILURES[stop_reason])

        if stop_reason != "end_turn":
            run.emit_exec(f"Deep run ended with an unknown stop reason: {stop_reason}", PHASE_ERROR)
            return DeepResult(
                met=False, output=output, tokens=run.tokens, cost_usd=run.cost_usd,
                error=(f"The ACP turn ended with an unrecognized stop reason ({stop_reason!r}), so "
                       "the goal is NOT confirmed met. Read the output for what it did."),
            )

        # Same silent-no-op guard the subprocess runner carries: a real turn that ran the goal
        # always produces agent text, so a clean end with nothing said means it never ran.
        if run.goal.strip() and not output.strip():
            run.emit_exec("Deep run ended cleanly but produced no output", PHASE_ERROR)
            return DeepResult(
                met=False, output=output, tokens=run.tokens, cost_usd=run.cost_usd,
                error=("the ACP turn ended cleanly but produced NO agent output, so the goal did "
                       "not actually run (check the agent binary and that the session was created "
                       "in the intended working directory)."),
            )

        notes = "; ".join(run.permission_notes)
        if notes:
            _log.info("acp run %s completed with denied permissions: %s", run.run_id, notes)
        run.emit_exec("Deep run finished", PHASE_DONE)
        return DeepResult(met=True, output=output, tokens=run.tokens, cost_usd=run.cost_usd)


def acp_model_family(model: Optional[str]) -> Optional[str]:
    """The ACP ``model`` config value for a QAR-resolved model id, or None to leave the default.

    QAR resolves a tier to a concrete id (``claude-sonnet-4-5-...``, or a Gemini/OpenAI id in a
    non-Claude deployment); the ACP agent selects by family. A non-Claude id maps to None, exactly
    as ``SubprocessGoalRunner`` omits ``--model`` for one — the Claude agent cannot run it either
    way, and forcing it would fail the run instead of quietly using a model it can.
    """
    m = (model or "").strip().lower()
    if not m:
        return None
    for family in ACP_MODEL_FAMILIES:
        if family in m:
            return family
    return None
