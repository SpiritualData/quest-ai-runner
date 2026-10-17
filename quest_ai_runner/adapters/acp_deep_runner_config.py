"""ACP deep runner, configuration and policy: Node and agent-binary discovery, ``AcpConfig`` and the
pure permission mapping (allow, reject or ask a human), all testable without a protocol session.

``adapters.acp_deep_runner`` re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.goal_runner import DEFAULT_MONITOR_HEARTBEAT_SECONDS, WEB_TOOLS
from .acp_deep_runner_protocol import (
    DEFAULT_AGENT_BINARY,
    field_of,
    log,
    MIN_NODE_MAJOR,
    QAR_ACP_AGENT_COMMAND_ENV_VAR,
    QAR_ACP_NODE_PATH_ENV_VAR,
)

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
        log.debug("could not probe node version at %s: %s", node_path, e)
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
