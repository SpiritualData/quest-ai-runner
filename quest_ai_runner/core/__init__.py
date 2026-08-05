"""quest_ai_runner.core — the domain-free orchestrator BRAIN.

This is the package a host application (a chat backend or cockpit) imports IN-PROCESS for
chat: construct an ``Orchestrator`` with adapters and call ``run``. It knows nothing about
Quest, any database, or any org — only the four adapter interfaces.
"""
from .adapters import (
    EVENT_CARD_THREAD,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_EXPLANATION,
    EVENT_MILESTONE,
    EVENT_MODE_SIGNAL,
    EVENT_OVERSEER,
    EVENT_PARTIAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_UNDERSTANDING,
    FUTURE_CONTEXT_VIA_FIELD,
    FUTURE_CONTEXT_VIA_OUTPUT,
    SURFACING_EVENTS,
    ConversationContext,
    ConversationStore,
    DeepResult,
    DeepRunner,
    DeepRunnerBase,
    Escalation,
    EscalationSink,
    EscalationSinkBase,
    FanoutSink,
    MilestoneSink,
    Mode,
    ModelProvider,
    ModelProviderBase,
    Observation,
    PlanDecision,
    ProgressEvent,
    ProgressSink,
    ProgressSinkBase,
    RetrievalAdapter,
    RetrievalAdapterBase,
    StreamSink,
)
from .goal_runner import (
    ESCALATION_MARKER,
    GoalRunner,
    SubprocessConfig,
    SubprocessGoalRunner,
    compose_goal_prompt,
    extract_escalation_id,
)
from .attachments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DESCRIBE_PROMPT,
    PreparedAttachment,
    PreparedAttachments,
    prepare_attachments,
)
from .model_registry import (
    DEFAULT_FALLBACK_TOP,
    TIERS,
    VISION_FAMILY_PATTERNS,
    ModelRegistry,
    bucket_top,
    is_vision_capable,
)
from .orchestrator import (
    BRAINSTORM_HELD_WORK_ACK_NOTE,
    BRAINSTORM_NO_ACTION_ACK_NOTE,
    DECIDE_TOOL,
    DEFERRED_RUNNER_KEY,
    MODE_RELEASE_PROMPT,
    MODE_RELEASE_TOOL,
    PLANNER_PROMPT,
    Orchestrator,
    OrchestratorConfig,
    OrchestratorResult,
    normalize_decision,
    planner_prompt_defaults,
    render_planner_prompt,
)
from .guard import (
    ExecutionFact,
    ExecutionRecord,
    classify_exec_phase,
)
from .overseer import (
    OVERSEE_TOOL,
    OVERSEER_PROMPT,
    OverseerSignal,
    build_digest,
    oversee,
)
from .card_thread import (
    ACTION_CONTINUE,
    ACTION_NEW,
    ACTION_SWITCH,
    FINISHED_STATUSES,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_COMPLETED,
    STATUS_DORMANT,
    CardCandidate,
    CardThreadContext,
    CardThreadDecision,
    card_id_set,
    find_duplicate_label,
    lifecycle_note,
    merge_candidates,
    normalize_label,
    parse_card_thread,
    penalized_budget,
    rank_card_first,
    render_thread_hint,
    select_thread_floor,
    split_by_card,
)
from .context_doctrine import CARD_LIFECYCLE_GATE, CARD_THREAD_GATE
from .turn_context_store import TurnContextStore
from .composite_assembler import CompositeContextAssembler

__all__ = [
    # adapters / value objects
    "RetrievalAdapter", "ModelProvider", "DeepRunner", "EscalationSink",
    "RetrievalAdapterBase", "ModelProviderBase", "DeepRunnerBase", "EscalationSinkBase",
    "Observation", "PlanDecision", "DeepResult", "Escalation",
    "FUTURE_CONTEXT_VIA_OUTPUT", "FUTURE_CONTEXT_VIA_FIELD",
    # two product modes + streaming/progress interface
    "Mode", "ProgressEvent", "ProgressSink", "ProgressSinkBase",
    "StreamSink", "MilestoneSink", "FanoutSink", "SURFACING_EVENTS",
    "EVENT_STATUS", "EVENT_PLAN", "EVENT_READ", "EVENT_REPLAN", "EVENT_PARTIAL",
    "EVENT_EXEC", "EVENT_RESULT", "EVENT_DECISION", "EVENT_MILESTONE", "EVENT_DONE",
    "EVENT_UNDERSTANDING", "EVENT_OVERSEER", "EVENT_MODE_SIGNAL", "EVENT_CARD_THREAD",
    "EVENT_EXPLANATION",
    # storage-agnostic conversation-history retrieval (User Input Understanding)
    "ConversationContext", "ConversationStore",
    # registry
    "ModelRegistry", "bucket_top", "TIERS", "DEFAULT_FALLBACK_TOP",
    "is_vision_capable", "VISION_FAMILY_PATTERNS",
    # attachments (multimodal handler)
    "prepare_attachments", "PreparedAttachments", "PreparedAttachment",
    "DESCRIBE_PROMPT", "DEFAULT_MAX_ATTACHMENT_BYTES",
    # orchestrator
    "Orchestrator", "OrchestratorConfig", "OrchestratorResult",
    "PLANNER_PROMPT", "DECIDE_TOOL", "normalize_decision",
    # render PLANNER_PROMPT without knowing its full slot set (non-breaking path for consumers)
    "render_planner_prompt", "planner_prompt_defaults",
    # EXECUTION MODES (the brainstorm no-action latch). A consumer's compat probe can key on these
    # STABLE public names to tell whether the library it loaded has the judged latch:
    #   * OrchestratorConfig fields ``execution_mode``, ``mode_signals_enabled``, ``mode_release_tier``
    #   * the exit authority ``Orchestrator.judge_brainstorm_release`` (+ its MODE_RELEASE_TOOL schema)
    #   * ``OrchestratorResult.mode_signal`` and the EVENT_MODE_SIGNAL event
    # A build without the latch lacks all of them, so a stale library can never look like it holds.
    "MODE_RELEASE_TOOL", "MODE_RELEASE_PROMPT",
    "BRAINSTORM_NO_ACTION_ACK_NOTE", "BRAINSTORM_HELD_WORK_ACK_NOTE",
    # PER-IDEA THREADING (the idea IS the card; see core/card_thread.py). A consumer's compat probe
    # can key on these STABLE public names to tell whether the library it loaded can thread ideas:
    #   * OrchestratorConfig field ``card_thread_enabled`` + the ``card_thread`` kwarg on
    #     ``Orchestrator.run``
    #   * ``CardThreadContext`` / ``CardThreadDecision`` / ``parse_card_thread`` (the fail-safe)
    #   * ``OrchestratorResult.card_thread`` and the EVENT_CARD_THREAD event
    # A build without threading lacks all of them, so a stale library can never look like it threads.
    "CardThreadContext", "CardThreadDecision", "CardCandidate",
    "parse_card_thread", "merge_candidates", "render_thread_hint",
    "select_thread_floor", "split_by_card", "rank_card_first", "penalized_budget",
    "card_id_set",
    "normalize_label", "find_duplicate_label", "lifecycle_note",
    "ACTION_CONTINUE", "ACTION_SWITCH", "ACTION_NEW",
    "STATUS_ACTIVE", "STATUS_COMPLETED", "STATUS_DORMANT", "STATUS_ARCHIVED", "FINISHED_STATUSES",
    "CARD_THREAD_GATE", "CARD_LIFECYCLE_GATE",
    # reserved deep_runners key for a queued deferred deployment
    "DEFERRED_RUNNER_KEY",
    # execution record (claim honesty is judged inside the goal verification)
    "ExecutionRecord", "ExecutionFact", "classify_exec_phase",
    # goal runner
    "GoalRunner", "SubprocessGoalRunner", "SubprocessConfig", "compose_goal_prompt",
    "ESCALATION_MARKER", "extract_escalation_id",
    # minimal-intervention overseer
    "OverseerSignal", "oversee", "build_digest", "OVERSEE_TOOL", "OVERSEER_PROMPT",
    # turn context store + composite assembler (replace TurnMemory)
    "TurnContextStore", "CompositeContextAssembler",
]
