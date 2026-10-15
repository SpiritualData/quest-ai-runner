"""Ports for deep work and its outputs: ``DeepRunner``, the writers, and the escalation and progress sink ports.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable
from dataclasses import dataclass

from .adapters_values import DeepResult, Escalation, ProgressEvent
from .adapters_events import Mode


@runtime_checkable
class DeepRunner(Protocol):
    """Spawn a bounded, goal-driven autonomous run (the ``/goal --max-turns`` contract).

    OPTIONAL attribute ``future_context_channel`` (``FUTURE_CONTEXT_VIA_OUTPUT`` by default, or
    ``FUTURE_CONTEXT_VIA_FIELD`` for a runner whose output is a strict format such as generated code
    or JSON): it selects how this runner hands back the FUTURE-CONTEXT bullets the async card updater
    learns from. See ``DeepRunnerBase.future_context_channel`` for the full contract. It is read with
    ``getattr(..., default)``, so it is deliberately NOT a required Protocol member: a runner that
    never declares it keeps today's prose behaviour.

    OPTIONAL attribute ``write_surface`` (one of ``WRITE_SURFACE_FILES``, ``WRITE_SURFACE_OPERATIONS``,
    ``WRITE_SURFACE_AGENT``; ``WRITE_SURFACE_AGENT`` by default): declares WHAT KIND of change this
    rung is capable of making -- files on disk only, named governed operations only, or (the
    full-agent default) either, including writing code. The orchestrator reads it with
    ``getattr(..., WRITE_SURFACE_AGENT)`` to keep a quest-data change off a rung that cannot satisfy
    it (see ``ladder_for_quest_data`` in ``core/orchestrator.py``). A runner that never declares it
    is treated as a full agent, i.e. today's behaviour.
    """

    def run_goal(
        self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
        emit: Optional[Callable[[ProgressEvent], None]] = None,
        context_preamble: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> DeepResult:
        """Run an autonomous worker toward ``goal`` (a written done-standard), bounded by
        ``max_turns``. Return a DeepResult distinguishing met-vs-limit. Never raises.

        ``emit`` (optional) lets a long-running deep runner report its EXECUTION LIFECYCLE as it
        works — generated code, each execution attempt, its raw output, retries, done — by emitting
        ``ProgressEvent(type=EVENT_EXEC, ...)``. The orchestrator routes these through the run's
        sink, so they show live (LIVE) and are dropped as chatter (BACKGROUND), exactly like other
        intermediate texture. Runners that don't stream may ignore it. The orchestrator only passes
        ``emit`` to runners whose ``run_goal`` accepts it, so older signatures keep working.

        ``context_preamble`` (optional) is a PER-CALL context preamble for THIS run only. The
        orchestrator forwards it (when the caller supplies one, e.g. an AI rep's pulled persona)
        so the run executes with that context without mutating any shared runner state. A runner
        that accepts it should prepend it ahead of its own configured preamble (or use it in
        place of one). Like ``emit``, it is passed ONLY to runners whose ``run_goal`` accepts it,
        so older signatures keep working and callers that pass nothing see prior behaviour.

        ``run_id`` (optional) is the STABLE id for this subgoal's whole retry sequence (the
        orchestrator generates it once per subgoal, before its first attempt). A runner that
        streams ``emit`` events should tag every event from every retry with this SAME id, since a
        retry may spawn an entirely new underlying process/session — without a stable id, a
        consumer's dashboard would otherwise show each retry as a separate duplicate run for what
        is really one ongoing subgoal. Passed ONLY to runners whose ``run_goal`` accepts it."""


@dataclass
class WriteResult:
    """The outcome of ONE attempted write through a ``FileWriter`` or an ``OperationWriter``.

    Deliberately a value, never an exception: a refused or failed write is an ordinary, expected
    outcome here (a path outside the root, a credential file, a read-only disk, an operation not
    on a write allowlist), and the caller's correct response is to report it and escalate, not to
    unwind. ``ok=False`` always means nothing changed: for a ``FileWriter`` the file on disk is
    UNCHANGED; for an ``OperationWriter`` the mutation was never attempted (a refusal) or the
    attempt failed (nothing on the far end changed).

    Shared by both write interfaces on purpose rather than each inventing its own result shape:
    ``rel_path`` doubles as "the path" for a file write and "the aliased operation name" for an
    ``OperationWriter`` call (e.g. ``"issues:create"``) -- both are just "the target, as the
    caller named it, for reporting". ``bytes_written``/``created``/``backup_path`` are FILE-write
    specific and left at their defaults (0/False/None) by an ``OperationWriter``, which instead
    fills ``detail`` with whatever adapter-specific payload it has (an MCP write's tool name and
    args, for auditability of what was -- or would have been -- executed).
    """
    ok: bool
    rel_path: str                          # the path (FileWriter) or aliased operation name
                                            # (OperationWriter) as the caller named it, for reporting
    error: Optional[str] = None            # why it was refused/failed; None on success
    bytes_written: int = 0
    created: bool = False                  # True = the file did not exist before this write
    backup_path: Optional[str] = None      # where the PREVIOUS content was saved, if anywhere
    detail: Optional[Dict[str, Any]] = None  # adapter-specific auditability payload (e.g. an


                                            # OperationWriter's {"tool": ..., "args": ...}); None
                                            # for a plain FileWriter result.


@runtime_checkable
class FileWriter(Protocol):
    """WRITE side of the corpus boundary — the only way anything in this library changes a file.

    This is a separate interface from ``RetrievalAdapter`` on purpose. Retrieval is a read
    capability and every reference implementation of it is strictly read-only; writing is a
    different capability with a different blast radius, so it gets its own interface rather than
    being bolted onto the read one. A consumer that wires no ``FileWriter`` has, by construction,
    no code path in this library that can modify its files.

    IT IS OPT-IN. Nothing constructs one automatically: the consumer has to build a writer and
    hand it over (``RunnerConfig.file_writer``), which is the act of granting write access. The
    reference implementation is ``adapters.files_writer.FilesWriter``.

    An implementation MUST:
      * confine every path to its configured root, resolving symlinks and ``..`` BEFORE the
        containment test (``adapters.files_adapter.resolve_in_tree`` is the shared implementation);
      * refuse credential-ish files exactly as the read side does;
      * never raise — every refusal and every I/O failure comes back as ``WriteResult(ok=False)``.
    """

    def read_file(self, rel_path: str) -> Optional[str]:
        """Return the file's full text, or None if it is missing/unreadable/out of bounds.

        Present on the WRITER (rather than reusing a RetrievalAdapter read) because an edit has to
        be computed against the exact bytes the write will replace, resolved through the same
        boundary that will accept or refuse the write.
        """

    def write_file(self, rel_path: str, content: str) -> WriteResult:
        """Replace ``rel_path``'s entire content with ``content``. Never raises."""


@runtime_checkable
class OperationWriter(Protocol):
    """WRITE side for a NAMED, SCHEMA-DESCRIBED mutating operation -- the general form of
    ``FileWriter`` for write capabilities broader than "replace a file's content" (send a
    message, create an issue, delete a row, ...). An MCP write tool is the motivating case, but
    nothing here is MCP-specific: any capability that mutates something outside a plain file is a
    NAMED operation with a schema, not a path+content pair, and belongs behind this interface
    instead of a special case inside the brain.

    Reuses ``WriteResult``'s value-not-exception contract rather than inventing a parallel result
    shape: ``ok=False`` always means nothing changed, ``error`` explains why, and ``detail``
    carries whatever adapter-specific auditability payload a caller needs (for an MCP operation,
    ``{"tool": ..., "args": ...}``) since a mutation here does not have a "path" the way a file
    write does.

    IT IS OPT-IN, same as ``FileWriter``: nothing constructs one automatically, and a consumer
    that wires none has, by construction, no code path in this library that can execute a
    mutating operation. The reference implementation is ``adapters.mcp_write_adapter.MCPWriteAdapter``.

    An implementation MUST:
      * refuse an operation not on its own write allowlist WITHOUT touching the network/backend at
        all (mirrors ``FileWriter``'s containment refusal, and mirrors ``RetrievalAdapter``'s own
        read-side allowlist refusal -- but a WRITE allowlist is its own, separate list: being
        allowlisted for READ grants nothing here);
      * never raise -- every refusal and every execution failure comes back as
        ``WriteResult(ok=False)``;
      * record which operation name and args were (or would have been) executed in
        ``WriteResult.detail``, for auditability, whether the call succeeded, failed, or was
        refused.
    """

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        """``[{"name", "description", "input_schema"}, ...]`` for operations this instance may
        execute (i.e. exactly its own write allowlist, schema-described). Best-effort discovery
        for a caller building a prompt or a UI; an implementation that cannot introspect its own
        catalog returns ``[]``. Never raises.
        """

    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult:
        """Execute ONE named mutating operation with ``args``. Never raises.

        Must refuse (``WriteResult(ok=False)``, no network/backend call) any ``name`` not on this
        instance's own write allowlist.
        """


@runtime_checkable
class EscalationSink(Protocol):
    """Raise a human-only confirm/decision and return its id."""

    def escalate(self, escalation: Escalation) -> str:
        """Create a decision-request (or equivalent) and return its decision id."""


@runtime_checkable
class ProgressSink(Protocol):
    """The "inform along the way" discipline — the ONE place messaging policy lives.

    The orchestrator routes EVERY internal event through ``update(event, mode)``. The sink
    decides what (if anything) surfaces to the consumer. The orchestrator never decides
    messaging policy itself; it only emits. Mode is passed so a single sink could vary its
    behavior, but the two reference sinks below are mode-shaped by construction:

      * ``StreamSink``    — LIVE: forwards every event to the consumer's stream.
      * ``MilestoneSink`` — BACKGROUND: drops planning/reading/re-planning chatter; forwards
                            only ``result`` / ``decision`` / ``milestone`` / ``done``.
    """

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        """Receive one event. Decide whether/how it surfaces. Must never raise."""
