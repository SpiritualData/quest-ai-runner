"""MCPOperationRunner -- a ``DeepRunner`` that executes ONE MCP mutating operation in one model call.

WHY IT EXISTS
-------------
Mirrors ``adapters.fast_edit_runner.FastEditRunner``'s role in the deep-runner ladder, but for an
``OperationWriter`` (``core/adapters.py``) instead of a ``FileWriter``: given a goal, brief, and
the context already gathered, it asks the model to pick AT MOST ONE writable MCP operation -- from
the schemas the writer itself discovers via ``list_writable_operations()`` -- and the arguments
for it, executes it through the writer, and returns. Same "one round trip instead of a full agent"
economics FastEditRunner exists for, and the same rung discipline: it is the FIRST attempt at a
"deep" goal this writer can satisfy directly, never the only path -- a goal it declines (no
matching operation, an ambiguous request, a failed call) falls through to whatever the next rung
(typically the full ``SubprocessGoalRunner``) can do instead.

WHERE IT SITS -- the SAME gate as FastEditRunner / FileWriter
---------------------------------------------------------------
Reachable ONLY through the orchestrator's deep-runner ladder, i.e. only once the planner has
already decided ``action: "deep"`` for this turn (``core.orchestrator.Orchestrator._run_deep``).
Nothing in the plan/gather/re-plan loop can reach this runner or the writer behind it -- exactly
like ``FastEditRunner`` and the ``FileWriter`` it wraps. That was a deliberate finding, not an
assumption: ``FileWriter.write_file`` today has exactly ONE caller in this whole library
(``FastEditRunner.apply_response``), and ``FastEditRunner`` is reachable only via
``config.resolve_deep_runner_ladder`` -> ``Orchestrator._run_deep``, which itself is reachable
only when the planner's structured decision is ``"deep"`` (CLAUDE.md hard rule #3: honor that
decision, never second-guess it from wording). This runner is wired into the SAME ladder, by the
SAME opt-in rule (present only when the consumer configured writable MCP operations -- see
``config.resolve_mcp_write_runners``), so MCP writes inherit that gate unchanged rather than
growing a parallel one.

WHAT KEEPS IT HONEST
---------------------
  * It can only call an operation the writer itself reports via ``list_writable_operations()`` --
    which is exactly the writer's own ``writable_tools`` allowlist (a DIFFERENT allowlist from any
    read-side ``allowed_tools``; see ``mcp_write_adapter.MCPWriteAdapter``). The model cannot widen
    its own blast radius by naming an operation outside that catalog: ``write_operation`` refuses
    it before touching the network, the same value-not-exception contract as ``FileWriter``.
  * The decision is FORCED STRUCTURED OUTPUT (the same ``ModelProvider.plan(tool_schema=...)``
    mechanism the main planner uses for its own ``action`` decision), not free prose scanned for
    keywords -- CLAUDE.md hard rule #3 applies here too. An explicit decline sentinel (``""``) is
    a value the model was told to fill in the schema, not a phrase inferred from wording.
  * If the model declines (no operation applies) it returns ``met=False`` and does nothing, which
    is always available and is the failure mode by design -- same discipline as FastEditRunner's
    "no candidate file" case.
  * A single attempt, no in-process retry loop: unlike a file edit there is no local diagnostic to
    retry against (the tool call ran or it did not), so a failed call escalates straight to the
    next rung rather than arguing with the same model again.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import (
    EVENT_EXEC,
    FUTURE_CONTEXT_VIA_FIELD,
    DeepResult,
    DeepRunnerBase,
    ModelProvider,
    OperationWriter,
    ProgressEvent,
)
from ..core.model_registry import ModelRegistry

_log = logging.getLogger("quest-ai-runner.mcp_write_runner")

PHASE_CATALOG = "catalog"
PHASE_DECIDING = "deciding"
PHASE_EXECUTING = "executing"
PHASE_DONE = "done"
PHASE_ERROR = "error"

# The explicit decline value the forced-schema decision may return. A STRUCTURED field the model
# was asked to fill (part of the tool_schema's enum), never a keyword scanned out of free prose --
# see CLAUDE.md hard rule #3 and NO_EDIT_SENTINEL's identical reasoning in fast_edit_runner.py.
DECLINE_SENTINEL = ""


def _decide_tool_schema(operation_names: List[str]) -> Dict[str, Any]:
    return {
        "name": "execute_operation",
        "description": (
            "Choose the ONE writable operation that satisfies the goal, with its arguments, "
            "or decline if none of the available operations genuinely apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": operation_names + [DECLINE_SENTINEL],
                    "description": "One of the operation names listed below, or \"\" to decline.",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments for the chosen operation, matching its own "
                                   "input_schema. An empty object when declining.",
                },
            },
            "required": ["operation", "args"],
        },
    }


@dataclass
class MCPOperationConfig:
    """Knobs for the fast MCP-operation path. Mirrors ``FastEditConfig``'s spirit."""
    # Model tier used when the orchestrator does not pin a model for the attempt. Not the
    # cheapest tier: this call decides a real mutating action, same reasoning as
    # ``FastEditConfig.tier``.
    tier: str = "quality"


class MCPOperationRunner(DeepRunnerBase):
    """One-call MCP write execution behind the ``DeepRunner`` interface.

    Args:
        provider: the model provider. MUST be the ``MultiProvider``-wrapped one
            (``build_orchestrator`` wraps ``cfg.model_provider`` in place before this runner is
            built), so the model id routes to whichever backend owns it -- same requirement as
            ``FastEditRunner``.
        writer: the ``OperationWriter`` granting write access (an ``MCPWriteAdapter``,
            typically). Required -- there is no default and none is constructed implicitly. No
            writer, no runner.
        registry: used to resolve ``config.tier`` when the caller does not pin a model. Built from
            the provider when not supplied.
    """

    # Its output is a mechanical execution report, not composed prose, and it knows the reusable
    # facts (which operation ran, with what args) exactly and for free -- same reasoning as
    # FastEditRunner's future_context_channel.
    future_context_channel = FUTURE_CONTEXT_VIA_FIELD

    def __init__(self, *, provider: ModelProvider, writer: OperationWriter,
                 registry: Optional[ModelRegistry] = None,
                 config: Optional[MCPOperationConfig] = None):
        self.provider = provider
        self.writer = writer
        self.registry = registry or ModelRegistry(provider)
        self.config = config or MCPOperationConfig()

    # --- DeepRunner API --------------------------------------------------------------------

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None) -> DeepResult:
        started = time.time()

        def tick(phase: str, text: str, **data: Any) -> None:
            if emit is None:
                return
            try:
                emit(ProgressEvent(type=EVENT_EXEC, text=text,
                                   data={"run_id": run_id, "phase": phase, **data}))
            except Exception:  # noqa: BLE001 -- streaming must never break the run
                pass

        try:
            catalog = self.writer.list_writable_operations()
            if not catalog:
                tick(PHASE_ERROR, "No writable MCP operation is available.")
                return DeepResult(
                    met=False,
                    output="No MCP operation was attempted: no writable operations are "
                           "available.",
                    error="mcp operation runner: no writable operations discovered")

            names = [c["name"] for c in catalog if c.get("name")]
            tick(PHASE_CATALOG, f"Found {len(names)} writable MCP operation(s).", operations=names)

            resolved_model = model or self.registry.resolve_tier(self.config.tier)
            prompt = self._build_prompt(goal=goal, brief=brief,
                                        context_preamble=context_preamble, catalog=catalog)

            tick(PHASE_DECIDING, "Choosing an MCP operation for this goal.")
            try:
                decision = self.provider.plan(prompt, model=resolved_model,
                                              tool_schema=_decide_tool_schema(names))
            except Exception as exc:  # noqa: BLE001 -- a DeepRunner never raises
                _log.warning("mcp operation runner: planner call failed", exc_info=True)
                tick(PHASE_ERROR, f"Planner call failed: {exc}")
                return DeepResult(
                    met=False, output="",
                    error=f"mcp operation runner: planner call failed: {type(exc).__name__}: {exc}")

            operation = str((decision or {}).get("operation") or "").strip()
            raw_args = (decision or {}).get("args")
            args = raw_args if isinstance(raw_args, dict) else {}

            if not operation or operation == DECLINE_SENTINEL:
                tick(PHASE_ERROR, "Declined: no available operation satisfies the goal.")
                return DeepResult(
                    met=False,
                    output="No MCP operation was attempted: none of the available operations "
                           "satisfy this goal.",
                    error="mcp operation runner: model declined (no matching operation)")

            tick(PHASE_EXECUTING, f"Executing {operation!r}.", tool=operation)
            result = self.writer.write_operation(operation, args)
            elapsed = time.time() - started

            if not result.ok:
                tick(PHASE_ERROR, f"Execution failed: {result.error}")
                return DeepResult(
                    met=False,
                    output=f"MCP operation {operation!r} was attempted and refused/failed: "
                           f"{result.error}",
                    error=f"mcp operation runner: {result.error}")

            tick(PHASE_DONE, f"Executed {operation!r}.", tool=operation)
            summary = (f"Executed MCP operation {operation!r} with args {json.dumps(args, default=str)} "
                      f"in {elapsed:.1f}s (one direct model call, no agent spawned).")
            return DeepResult(
                met=True, output=summary,
                future_context=f"- Executed MCP operation {operation!r} with args "
                               f"{json.dumps(args, default=str)}.")
        except Exception as exc:  # noqa: BLE001 -- a DeepRunner must never raise
            _log.debug("mcp operation runner failed: %s", exc)
            return DeepResult(met=False, output="", error=f"mcp operation runner error: {exc}")

    # --- prompt ------------------------------------------------------------------------------

    @staticmethod
    def _build_prompt(*, goal: str, brief: str, context_preamble: Optional[str],
                      catalog: List[Dict[str, Any]]) -> str:
        parts: List[str] = [f"GOAL (the done-standard for this action):\n{goal}"]
        if brief and brief.strip() != goal.strip():
            parts.append(f"FULL REQUEST AND BRIEF:\n{brief}")
        if context_preamble:
            parts.append("CONTEXT ALREADY GATHERED (do not go looking for more; this is what is "
                         f"known):\n{context_preamble}")
        lines = ["AVAILABLE OPERATIONS (choose at most one; decline if none genuinely apply):"]
        for c in catalog:
            schema = json.dumps(c.get("input_schema") or {}, default=str)
            lines.append(f"- {c['name']}: {c.get('description') or ''}\n  input_schema: {schema}")
        parts.append("\n".join(lines))
        return "\n\n".join(parts)
