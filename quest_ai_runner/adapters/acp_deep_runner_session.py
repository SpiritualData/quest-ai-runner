"""ACP deep runner, the live session: the ``open_agent_connection`` seam (the only place that
touches the ``acp`` SDK), ``AcpRun`` (the state shared between the async driver and the ACP
callbacks, including the steering channel) and ``AcpSessionClient`` (the ACP client callbacks).

``adapters.acp_deep_runner`` re-exports every name here.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

from ..core.adapters import Escalation, EVENT_EXEC, ProgressEvent
from .acp_deep_runner_config import (
    AcpConfig,
    ALLOW,
    classify_permission,
    HUMAN,
    permission_response,
    REJECT,
    select_option,
)
from .acp_deep_runner_protocol import (
    AcpUnavailable,
    chunk_text,
    field_of,
    landed_file_change,
    log,
    MAX_MESSAGE_CHARS,
    PHASE_ESCALATED,
    PHASE_STEER,
    PHASE_TOOL_PROGRESS,
    STEERING_EXT_METHOD,
    STEERING_IDLE_BEHAVIOR,
    tool_name_of,
    translate_session_update,
    truncate,
    usage_from_update,
)

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
        # What the agent's own tool-call updates say it changed: the ids of the file-changing calls that completed
        # (``landed_file_change``), with the kind and name each call announced. Becomes ``DeepResult.changes_landed``.
        self.landed_changes: Set[str] = set()
        self.tool_kinds: Dict[str, str] = {}
        self.tool_names: Dict[str, str] = {}
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
            log.debug("acp progress sink raised; continuing", exc_info=True)

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
            log.debug("acp steering drain failed; continuing", exc_info=True)
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
            log.debug("acp steering return failed; message not requeued", exc_info=True)
        log.warning("acp steering: message could not be injected and had nowhere to go back to")

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
            log.warning("acp steering request failed: %s", e)
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
        log.info("acp steering not injected (outcome=%r); message returned to the queue", outcome)
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
            log.warning("acp permission escalation failed (%s); denying the tool instead", e)
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
            landed = landed_file_change(update, run.tool_kinds, run.tool_names)
            if landed:
                run.landed_changes.add(landed)
            usage = usage_from_update(update)
            if usage is not None:
                if usage.get("cost_usd"):
                    run.cost_usd = float(usage["cost_usd"])
                return
            if field_of(update, "session_update", "sessionUpdate") == "agent_message_chunk":
                run.add_agent_text(field_of(update, "message_id", "messageId"), chunk_text(update))
            payload = translate_session_update(update)
            if payload:
                run.emit_exec(payload["text"], payload["phase"], **(payload.get("data") or {}))
        except Exception:  # noqa: BLE001 — a translation bug must not kill the session
            log.debug("acp session_update handling failed; continuing", exc_info=True)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kwargs: Any) -> Dict[str, Any]:
        try:
            return await self.run.decide_permission(tool_call, options)
        except Exception:  # noqa: BLE001 — never answer a permission ask with an exception
            log.warning("acp permission handling failed; cancelling the ask", exc_info=True)
            return permission_response(None)

    async def ext_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Agent-initiated extension requests. We implement none; answer benignly rather than
        erroring, so an agent probing an extension never fails the run."""
        log.debug("acp: ignoring agent extension request %r", method)
        return {}

    async def ext_notification(self, method: str, params: Dict[str, Any]) -> None:
        log.debug("acp: ignoring agent extension notification %r", method)
