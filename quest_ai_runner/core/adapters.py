"""Adapter interfaces — the generic boundary between the brain and any org's specifics.

The orchestrator BRAIN (``core.orchestrator``), the model registry, and the goal-runner
know NOTHING about any specific org, Quest, a database, the filesystem layout, or any user.
They only know these five Protocols. A consumer supplies concrete implementations via
``config.RunnerConfig`` — reference implementations live in ``quest_ai_runner.adapters``.

The five roles:

  * ``RetrievalAdapter`` — GATHER. ``read_section`` / ``grep`` / ``query`` over whatever
    the consumer's source of truth is (indexed files, a live DB, a dev server). This is what
    the brain's plan->read->re-plan loop calls. The brain never opens a file or a socket itself.

  * ``ModelProvider`` — the LLM. ``plan`` (one cheap structured planner decision), ``answer``
    (generate a grounded reply), and ``list_models`` (the live id list the model_registry
    buckets into tiers). Default reference impl wraps the Anthropic SDK.

  * ``DeepRunner`` — spawn a bounded, goal-driven autonomous run (the ``/goal --max-turns``
    contract). The consumer plugs in Claude Code, another agent, or a mock. Returns a
    ``DeepResult`` distinguishing goal-MET from limit/error.

  * ``EscalationSink`` — raise a human-only confirm/decision (the Quest team decision-request
    in production; a recording stub in tests). Returns a decision id the runner reports back.

  * ``ContextAssembler`` — PRE-FLIGHT CONTEXT. Called ONCE before the loop, guaranteed, when
    wired. Assembles task-specific context from a card store / index and feeds it to
    ``run(context_view=...)`` before planning starts. Optional: consumers that omit it get
    exactly today's reactive-gather behaviour.

  * ``VectorStore`` — OPTIONAL VECTOR ORIENTATION. Embeds items and queries, upserts points
    into a collection scoped by org/team/quest, and retrieves semantically similar candidates.
    Complements keyword/IDF (``FileContextStore``) rather than replacing it: keyword catches
    exact identifiers; vectors catch semantics. The ``sync`` method is the AUTO-UPDATE entry
    point: it re-embeds only items whose fingerprint changed, so the index stays fresh with
    zero user management. Heavy deps (qdrant-client, fastembed) are optional; the Protocol is
    defined here in stdlib so the core stays dependency-free.

All five are ``typing.Protocol`` so a consumer can satisfy them structurally (duck typing) —
no inheritance required — but ABCs (``RetrievalAdapterBase`` etc.) are also offered for
implementers who prefer explicit subclassing. Keeping these tiny and dependency-free is the
whole point: a stranger's org can adopt the library by implementing five small surfaces.

The definitions live in ``adapters_<role>`` modules (``_scope``, ``_events``, ``_values``,
``_retrieval``, ``_model``, ``_deep``, ``_context``, ``_guidance``, ``_bases``, ``_sinks``,
``_channels``); this module re-exports every name so ``from .adapters import X`` keeps working.
Put a new port in the module that owns its role. ``_scope``, ``_model`` and ``_context`` are themselves thin re-exports
of ``hubofhubs.ports`` (the neutral ports moved to the hub package); the other modules hold ports that carry this
library's own value objects and events and stay here.
"""
from __future__ import annotations

import contextlib
import enum
import re
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol, runtime_checkable
import abc

from hubofhubs.ports.scope import (  # noqa: F401
    ScopedCounter,
    ScopedThreadPoolExecutor,
    ThreadLocalCounter,
    _usage_scope_state,
    bound_usage_scope,
    current_usage_scope,
)
from .adapters_events import (  # noqa: F401
    EVENT_CARD_THREAD,
    EVENT_CONTEXT,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_EXEC,
    EVENT_EXPLANATION,
    EVENT_INTENT,
    EVENT_MILESTONE,
    EVENT_MODE_SIGNAL,
    EVENT_OVERSEER,
    EVENT_PARTIAL,
    EVENT_PLAN,
    EVENT_READ,
    EVENT_REPLAN,
    EVENT_RESULT,
    EVENT_STATUS,
    EVENT_TOKENS,
    EVENT_UNDERSTANDING,
    Mode,
    SURFACING_EVENTS,
)
from .adapters_values import (  # noqa: F401
    DeepResult,
    Escalation,
    FUTURE_CONTEXT_VIA_FIELD,
    FUTURE_CONTEXT_VIA_OUTPUT,
    LAUNCH_MISSING_PROGRAM,
    LAUNCH_PERMISSION_DENIED,
    Observation,
    PlanDecision,
    ProgressEvent,
    RELATIVE_DEADLINE_RE,
    WRITE_SURFACE_AGENT,
    WRITE_SURFACE_FILES,
    WRITE_SURFACE_OPERATIONS,
    parse_deadline,
)
from .adapters_retrieval import (  # noqa: F401
    RetrievalAdapter,
    WebResearch,
)
from hubofhubs.ports.model import (  # noqa: F401
    ModelProvider,
    STEP_JUDGE,
    STEP_PLAN,
    STEP_NARRATION,
    STEP_REPLY,
    STEP_SELECT,
    STEP_SUMMARIZE,
    STEP_UNDERSTAND,
    STEP_VERIFY,
    accepts_reasoning_hint,
    accepts_step_hint,
    answer_with_reasoning,
    plan_with_step,
)
from .adapters_deep import (  # noqa: F401
    DeepRunner,
    EscalationSink,
    FileWriter,
    OperationWriter,
    ProgressSink,
    WriteResult,
)
from hubofhubs.ports.context import (  # noqa: F401
    AssembledContext,
    ContextAssembler,
    ConversationContext,
    ConversationStore,
    VectorHit,
    VectorStore,
)
from .adapters_guidance import (  # noqa: F401
    GuidanceCard,
    GuidanceProvider,
)
from .adapters_bases import (  # noqa: F401
    ContextAssemblerBase,
    DeepRunnerBase,
    EscalationSinkBase,
    FileWriterBase,
    GuidanceProviderBase,
    ModelProviderBase,
    OperationWriterBase,
    ProgressSinkBase,
    RetrievalAdapterBase,
    VectorStoreBase,
    runner_starts_background_work,
    runner_uses_deep_model,
)
from .adapters_sinks import (  # noqa: F401
    FanoutSink,
    MilestoneSink,
    StreamSink,
)
from .adapters_channels import (  # noqa: F401
    ChannelTransport,
    ChannelTransportBase,
    InboundBatch,
    InboundMessage,
    OutboundReply,
    SendResult,
)



