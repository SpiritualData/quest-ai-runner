"""quest_ai_runner.core — the domain-free orchestrator BRAIN.

This is the package a host application (a chat backend or cockpit) imports IN-PROCESS for
chat: construct an ``Orchestrator`` with adapters and call ``run``. It knows nothing about
Quest, any database, or any org — only the four adapter interfaces.
"""
from .adapters import (
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_MILESTONE,
    EVENT_OVERSEER,
    EVENT_PARTIAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_UNDERSTANDING,
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
    DECIDE_TOOL,
    PLANNER_PROMPT,
    Orchestrator,
    OrchestratorConfig,
    OrchestratorResult,
    normalize_decision,
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
from .turn_context_store import TurnContextStore
from .composite_assembler import CompositeContextAssembler

__all__ = [
    # adapters / value objects
    "RetrievalAdapter", "ModelProvider", "DeepRunner", "EscalationSink",
    "RetrievalAdapterBase", "ModelProviderBase", "DeepRunnerBase", "EscalationSinkBase",
    "Observation", "PlanDecision", "DeepResult", "Escalation",
    # two product modes + streaming/progress interface
    "Mode", "ProgressEvent", "ProgressSink", "ProgressSinkBase",
    "StreamSink", "MilestoneSink", "FanoutSink", "SURFACING_EVENTS",
    "EVENT_STATUS", "EVENT_PLAN", "EVENT_READ", "EVENT_REPLAN", "EVENT_PARTIAL",
    "EVENT_EXEC", "EVENT_RESULT", "EVENT_DECISION", "EVENT_MILESTONE", "EVENT_DONE",
    "EVENT_UNDERSTANDING", "EVENT_OVERSEER",
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
