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
import os
import threading
import time

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from esoi import implementation
from ..core.requester import apply_requester_env
from ..core.adapters import DeepResult, DeepRunnerBase, ProgressEvent
from ..core.goal_runner import (
    compose_goal_prompt,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    extract_escalation_id,
    resolve_deep_timeout_seconds,
)
from .acp_deep_runner_config import AcpConfig, build_agent_argv, resolve_node_binary
from .acp_deep_runner_protocol import (
    ACP_MODEL_FAMILIES,
    ACP_PROTOCOL_VERSION,
    AcpUnavailable,
    EFFORT_CONFIG_ID,
    field_of,
    log,
    meta_of,
    MODEL_CONFIG_ID,
    PHASE_DONE,
    PHASE_ERROR,
    PHASE_HEARTBEAT,
    PHASE_SESSION,
    STOP_REASON_FAILURES,
    usage_from_prompt_response,
)
from .acp_deep_runner_session import AcpRun, AcpSessionClient, open_agent_connection

# ---------------------------------------------------------------------------------------------
# The DeepRunner
# ---------------------------------------------------------------------------------------------

@implementation("run-deep-work", "acp")
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

    config_class = AcpConfig   # the dataclass this runner is built from (core.resolve.resolve_config)

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
                log.warning("acp steer() failed for run %s: %s", run.run_id, e)
        return injected

    # --- DeepRunner ----------------------------------------------------------------------
    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None,
                 working_dir: Optional[str] = None,
                 resume_session_id: Optional[str] = None,
                 requester_user_id: Optional[str] = None) -> DeepResult:
        """Run ``goal`` as one ACP turn. Signature matches ``SubprocessGoalRunner.run_goal`` exactly.

        ``max_turns`` is accepted for interface parity but is NOT enforceable over ACP today: the
        protocol has no turn budget and the Claude ACP agent exposes none. The run is bounded by the
        wall-clock timeout here and by the orchestrator's own goal loop (attempts + token budget),
        which is where the real bound lives for both runners anyway.

        ``resume_session_id`` is accepted for the same parity reason and is likewise a no-op here.
        The goal loop only ever offers a continuation to a run that reported ``limit_hit`` — the
        worker's own "I ran out of turns" — and a runner with no turn budget never reports it, so
        an ACP run is never asked to resume. Each ACP turn starts a fresh session, as before.
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
                                      working_dir=effective_working_dir, timeout=timeout,
                                      requester_user_id=requester_user_id, run_id=run_id)
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
        # What the agent's tool calls show it changed, kept whatever way the run ended: a change made before a failure is
        # still a change.
        landed = len(run.landed_changes)
        if "error" in box:
            e = box["error"]
            log.error("acp deep run failed: %s: %s", type(e).__name__, e, exc_info=True)
            return DeepResult(met=False, error=f"ACP deep run failed: {type(e).__name__}: {e}", changes_landed=landed)
        result = box.get("result")
        if not isinstance(result, DeepResult):
            return DeepResult(met=False, error="ACP deep run produced no result", changes_landed=landed)
        result.changes_landed = landed
        return result

    # --- internals -----------------------------------------------------------------------
    def _register(self, run: AcpRun) -> None:
        with self._runs_lock:
            self._runs[run.key] = run

    def _unregister(self, run: AcpRun) -> None:
        with self._runs_lock:
            self._runs.pop(run.key, None)

    def build_env(self, requester_user_id: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, str]:
        """The child's environment. Mirrors ``SubprocessGoalRunner._build_env``: the spawned agent
        must not inherit our own Claude Code session or API billing, so those keys are dropped and
        it authenticates the same way the reference deep runner's worker does. The run's id rides in
        ``QAR_TOOL_RUN_ID`` so a tool call from the agent's shell lands on this run's record."""
        env = os.environ.copy()
        apply_requester_env(env, requester_user_id)
        if run_id:
            env["QAR_TOOL_RUN_ID"] = run_id
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
                           model: Optional[str], working_dir: str, timeout: float,
                           requester_user_id: Optional[str] = None, run_id: Optional[str] = None) -> DeepResult:
        run.loop = asyncio.get_running_loop()
        client = AcpSessionClient(run)
        try:
            async with open_agent_connection(client, argv, env=self.build_env(requester_user_id, run_id), cwd=working_dir) as (conn, _proc):
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
            log.error("acp session failed: %s: %s", type(e).__name__, e, exc_info=True)
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
            log.info("acp agent does not advertise steering; mid-run injection is unavailable")

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
            log.debug("acp: %r is not a Claude family the agent offers; using its default", model)

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
                    log.debug("acp: could not set session mode %r: %s", self.cfg.permission_mode, e)
            else:
                log.debug("acp: session does not offer mode %r; leaving the agent's default",
                           self.cfg.permission_mode)

    async def _safe_set_option(self, conn: Any, run: AcpRun, config_id: str, value: str) -> None:
        try:
            await conn.set_config_option(config_id=config_id, session_id=run.session_id, value=value)
        except Exception as e:  # noqa: BLE001 — a rejected option leaves the agent's default
            log.debug("acp: could not set config option %s=%s: %s", config_id, value, e)

    async def _safe_cancel(self, conn: Any, session_id: Optional[str]) -> None:
        if not session_id:
            return
        try:
            await conn.cancel(session_id=session_id)
        except Exception as e:  # noqa: BLE001
            log.debug("acp: cancel failed: %s", e)

    async def _safe_close(self, conn: Any, session_id: Optional[str]) -> None:
        if not session_id:
            return
        close = getattr(conn, "close_session", None)
        if close is None:
            return
        try:
            await close(session_id=session_id)
        except Exception as e:  # noqa: BLE001 — the process teardown closes it either way
            log.debug("acp: close_session failed: %s", e)

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
            log.info("acp run %s completed with denied permissions: %s", run.run_id, notes)
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


# The module is split by role; these names stay importable from here.
from .acp_deep_runner_protocol import (  # noqa: F401  (kept importable from here)
    ACP_MODEL_FAMILIES,
    ACP_PROTOCOL_VERSION,
    AcpUnavailable,
    DEFAULT_AGENT_BINARY,
    EFFORT_CONFIG_ID,
    MAX_MESSAGE_CHARS,
    MAX_THOUGHT_CHARS,
    MIN_NODE_MAJOR,
    MISSING,
    MODEL_CONFIG_ID,
    PHASE_DONE,
    PHASE_ERROR,
    PHASE_ESCALATED,
    PHASE_HEARTBEAT,
    PHASE_MESSAGE,
    PHASE_PLAN,
    PHASE_SESSION,
    PHASE_STEER,
    PHASE_THINKING,
    PHASE_TOOL_CALL,
    PHASE_TOOL_ERROR,
    PHASE_TOOL_PROGRESS,
    PHASE_TOOL_RESULT,
    QAR_ACP_AGENT_COMMAND_ENV_VAR,
    QAR_ACP_NODE_PATH_ENV_VAR,
    STEERING_EXT_METHOD,
    STEERING_IDLE_BEHAVIOR,
    STOP_REASON_FAILURES,
    TOOL_STATUS_PHASES,
    chunk_text,
    field_of,
    meta_of,
    tool_line,
    tool_name_of,
    translate_session_update,
    truncate,
    usage_from_prompt_response,
    usage_from_update,
)
from .acp_deep_runner_config import (  # noqa: F401  (kept importable from here)
    ALLOW,
    AcpConfig,
    HUMAN,
    REJECT,
    build_agent_argv,
    classify_permission,
    node_major_version,
    permission_response,
    resolve_agent_entry,
    resolve_node_binary,
    select_option,
)
from .acp_deep_runner_session import (  # noqa: F401  (kept importable from here)
    AcpRun,
    AcpSessionClient,
    open_agent_connection,
)
