"""Orchestrator brain — the BOUNDED ITERATIVE LOOP (plan -> gather -> re-plan -> answer/deep/confirm).

A generic orchestration loop, with no org/Quest/DB specifics baked in. The brain runs a
bounded loop per request: a fast PLANNER (one cheap structured
model call) AUTO-DECIDES the next step, given the message + everything gathered so far. The loop
lets the brain narrow in like Claude Code does: grep -> read the matching section -> re-plan ->
answer. The planner returns one of four actions each step:

  * read    — TARGETED partial reads/greps via the RetrievalAdapter (batched, run CONCURRENTLY).
              Appended to ``gathered``; the loop RE-PLANS with what it just saw.
  * answer  — reply now, grounded in ``gathered``. May fan out 2-N independent sub-questions,
              answered CONCURRENTLY then synthesized.
  * deep    — needs real work: author a concrete ``goal`` (checkable done-standard) + brief and
              hand to the DeepRunner. May fan out 2-N independent deep subtasks CONCURRENTLY.
  * confirm — a human-only / risky step: raise it via the EscalationSink and stop.

BOUNDING: capped at ``max_steps`` plus a wall-clock + gathered-size budget. If it hits the cap
without answering, it makes a best-effort grounded answer from ``gathered``, or escalates to a
deep run if nothing useful was gathered.

It is **domain-free**: it takes ADAPTERS (a RetrievalAdapter, a ModelProvider, a ModelRegistry,
and optional DeepRunner + EscalationSink), never hardcoded paths. The result is a structured
``OrchestratorResult`` the caller (in-process chat, or the runner's executor) turns into a chat
message, a deep run, or a Quest decision-request.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from hubofhubs.ports.scope import ScopedThreadPoolExecutor as ThreadPoolExecutor
from hubofhubs.ports.context import ContextAssembler, ConversationStore
from hubofhubs.ports.model import ModelProvider
from .adapters_events import EVENT_CONTEXT, EVENT_PARTIAL, Mode
from .adapters_deep import DeepRunner, EscalationSink
from .adapters_guidance import GuidanceProvider
from .adapters_values import PlanDecision, ProgressEvent
from .adapters_retrieval import RetrievalAdapter
from .anticipation import Anticipator
from .inbox import InputInbox
from .resolve import resolve
from .tools import ToolRegistry

from .recipes import RecipeStore
from hubofhubs.critique.link_guard import LinkGuard
from hubofhubs.models.family import provider_name_for_model
from .model_registry import ModelRegistry

# Log labels for where a deep-worker ladder came from (see Orchestrator.log_deep_ladder).


# Planner-context rendering lives in planner_context.py; re-exported so existing imports keep working.
from .planner_context import (
    PLANNER_WEB_HEAD,
    PLANNER_LINKS_HEAD,
    PLANNER_LINKS_NO_READER,
    LINK_NOT_READ_MARKER,
    LINK_NOT_READ_ANSWER_RULE,
    planner_links_block,
    _render_gathered_for_planner,
)
# Auxiliary prompt text and tool schemas live in auxiliary_prompts.py; re-exported so existing imports keep working.
from .auxiliary_prompts import (
    VERIFY_GOAL_TOOL,
    VERIFY_GOAL_PROMPT,
    MODE_RELEASE_TOOL,
    MODE_RELEASE_PROMPT,
    FUTURE_CONTEXT_DELIMITER,
    DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION,
    QUEST_DATA_WRITE_SURFACE_INSTRUCTION,
    REPLY_VOICE_SYSTEM,
)
from .recent_context import RecentContextStore
from .planner_prompt import (
    BRAINSTORM_HELD_WORK_ACK_NOTE,
    BRAINSTORM_NO_ACTION_ACK_NOTE,
    DEFERRED_DEEP_INLINE_SEMANTICS,
    DEFERRED_DEEP_QUEUED_SEMANTICS,
    PLANNER_DECISION_RUBRIC,
    PLANNER_PROFILES,
    PLANNER_PROMPT,
    PLANNER_PROMPT_COMPACT,
)

from .orchestrator_constants import (
    DEEP_CONTINUATION_TURN_MULTIPLIER_CAP,
    log,
)
from .orchestrator_types import (  # noqa: F401
    OrchestratorConfig,
    OrchestratorResult,
)
from .orchestrator_decide_tool import (
    DECIDE_TOOL,
    DECIDE_TOOL_WITH_MODE_SIGNAL,
    DEFERRED_RUNNER_KEY,
    decide_tool_for,
    planner_prompt_defaults,
    render_planner_prompt,
)
from .orchestrator_decisions import (
    apply_quest_data_ladder_guard,
    normalize_decision,
    parse_goal_condition_reply,
)
from .orchestrator_text import (  # noqa: F401
    _card_update_store,
    _deep_future_context,
    _future_context_channel,
    _normalize_future_context,
    _strip_future_context,
    sentence,
    unconfirmed_no_change_text,
)
from .orchestrator_events import (
    _grounding_block,
    _oversee_worth_a_look,
    _project_card_metadata_for_event,
    _project_sources_for_event,
    grounding_answer_tail,
)
from .orchestrator_capabilities import (
    context_assembly_timeout_seconds,
    guidance_selection_timeout_seconds,
    read_op_timeout_seconds,
    truncate_verify_context,
    verify_context_max_chars,
)
from .orchestrator_turn import Narrator, TurnCardCache, _Emitter
from .orchestrator_reads import ReadsMixin
from .orchestrator_recipes import RecipesMixin
from .orchestrator_planning import PlanningMixin
from .orchestrator_answering import AnsweringMixin
from .orchestrator_judges import JudgesMixin
from .orchestrator_deep_models import DeepModelsMixin
from .orchestrator_deep_context import DeepContextMixin
from .orchestrator_deep_run import DeepRunMixin
from .orchestrator_cards import CardsMixin
from .orchestrator_oversee import OverseeMixin
from .orchestrator_understanding import UnderstandingMixin
from .orchestrator_run import RunMixin


class Orchestrator(ReadsMixin, RecipesMixin, PlanningMixin, AnsweringMixin, JudgesMixin, DeepModelsMixin, DeepContextMixin, DeepRunMixin, CardsMixin, OverseeMixin, UnderstandingMixin, RunMixin):
    """The domain-free brain. Construct with adapters; call ``run`` per request."""

    def __init__(
        self,
        *,
        retrieval: RetrievalAdapter,
        provider: ModelProvider,
        registry: ModelRegistry,
        deep_runner: Optional[DeepRunner] = None,
        deep_runner_ladder: Optional[List[Any]] = None,
        deep_runners: Optional[Dict[str, Any]] = None,
        deep_runner_classifier: Optional[Any] = None,
        escalation: Optional[EscalationSink] = None,
        config: Optional[OrchestratorConfig] = None,
        status: Optional[Callable[[str], None]] = None,
        vision_provider: Optional[ModelProvider] = None,
        vision_model: Optional[str] = None,
        context_assembler: Optional[ContextAssembler] = None,
        guidance: Optional[GuidanceProvider] = None,
        input_inbox: Optional["InputInbox"] = None,
        conversation_store: Optional[ConversationStore] = None,
        recent_context: Optional[RecentContextStore] = None,
        anticipator: Optional[Anticipator] = None,
        tools: Optional[ToolRegistry] = None,
        web: Optional[Any] = None,
        recipes: Optional[RecipeStore] = None,
        goal_iteration: Optional[Any] = None,
        edited_files_card_updater: Optional[Callable[..., None]] = None,
        background_thread_registrar: Optional[Callable[[threading.Thread, Any], None]] = None,
    ):
        self.retrieval = retrieval
        self.provider = provider
        self.registry = registry
        self.deep_runner = deep_runner
        # THE DEEP-RUNNER LADDER: the ordered runners ONE goal may be tried with, cheapest rung
        # first. It defaults to exactly ``[deep_runner]`` (or ``[]`` when nothing is wired), which
        # is today's behaviour byte for byte: one rung, resolved once per task, used for every
        # attempt.
        #
        # A consumer that opts into fast in-process editing (see config.resolve_deep_runner_ladder)
        # gets ``[FastEditRunner, SubprocessGoalRunner]``, and NO new escalation logic is needed to
        # exploit it: the goal loop below already runs an attempt, verifies it against the written
        # done-standard, and retries on failure. The ladder is simply indexed by attempt number the
        # same way the MODEL ladder already is, so attempt 1 runs the cheap rung and a rung that
        # fails verification hands the next attempt to the next rung.
        self.deep_runner_ladder: List[Any] = (
            list(deep_runner_ladder) if deep_runner_ladder
            else ([deep_runner] if deep_runner is not None else []))
        # Named deep-runner registry + classifier (both optional). When both are set, the
        # classifier selects a runner key for each goal. Falls back to deep_runner on any
        # failure or missing key. All existing behaviour is unchanged when either is absent.
        self.deep_runners: Dict[str, Any] = deep_runners or {}
        self.deep_runner_classifier: Optional[Any] = deep_runner_classifier
        # Generic conversation inbox for mid-run user messages. When wired, ``run`` auto-drains the
        # current conversation's pending messages between goal-loop steps, so ANY interface that
        # pushes to it (chat, Quest frontend, ...) gets mid-run message folding with no extra wiring.
        self.input_inbox = input_inbox
        self.escalation = escalation
        self.cfg = config or OrchestratorConfig()
        self._status = status or (lambda _msg: None)
        # The describer used for the image describe-fallback path (a non-vision answering model, or
        # a text-only provider like the keyless CLI). When None, ``prepare_attachments`` reuses the
        # answering provider/model if that is itself vision-capable; otherwise images degrade to
        # honest notes. A consumer wiring a keyless answering provider should pass a vision-capable
        # ``vision_provider`` here so chat images are transcribed rather than dropped.
        self.vision_provider = vision_provider
        # The model id the ``vision_provider`` should use to DESCRIBE images. Required whenever the
        # vision_provider is a DIFFERENT provider from the answering one: the answering model id is
        # foreign to the describer (e.g. a Gemini/tier-alias answer id handed to an Anthropic
        # describer would 404), so a consumer must name the describer's own model here. When None,
        # ``prepare_attachments`` falls back to the answering model id (correct only when the
        # describer IS the answering provider).
        self.vision_model = vision_model
        # Optional PRE-FLIGHT CONTEXT adapter (the fifth adapter role). When wired, assemble() is
        # called once before the loop to inject task-specific context, and record() is called
        # after the run completes as a best-effort write-back. Never raises in either direction.
        self.context_assembler = context_assembler
        # Optional USE-CASE-SPECIFIC INSTRUCTIONS adapter (the GuidanceProvider role). When wired,
        # the orchestrator pre-selects the cards most relevant to the user's message into an
        # "APPLICABLE GUIDANCE" block before planning, and the planner may list_guidance /
        # read_guidance on demand. Cards are opaque text. Never raises. None = today's behavior.
        self.guidance = guidance
        # The LINK GUARD, built once on first use and then kept for the life of this orchestrator
        # so its verdict cache survives across turns: a long-lived poller checks a given URL once
        # an hour, not once a reply. See resolve_link_guard() and hubofhubs.critique.link_guard.
        self.link_guard_instance: Optional[LinkGuard] = None
        self.link_guard_built = False
        # Optional storage-agnostic CONVERSATION STORE (the User Input Understanding step). When
        # wired AND the caller passes a conv_id, the brain may pull a relevant slice of the current
        # (and related) conversation to rewrite a short/anaphoric message into a self-contained goal
        # condition before selecting context. Never raises. None = Step 1 is a no-op (zero latency).
        self.conversation_store = conversation_store
        # Optional WARM RECENT-CONTEXT store (see core/recent_context.py). When wired, ``run`` (and
        # each deep goal via ``_assemble_for_goal``) loads the cards recently selected across THREE
        # scopes -- this conversation, this quest, and always "global" -- (fast, no LLM), gates them
        # through a pure lexical relevance filter weighted by scope, and merges any survivors into
        # context_view so a follow-up turn (or a background task) is warm even before/without a
        # fresh assembly. It also remembers, PER CARD, which content items past turns found useful
        # for a similar input, ranking them to the front both when rendering a carried-over card and
        # as a hint to the consolidating LLM pass when a card is re-found by fresh assembly. Never
        # raises. None = exactly today's behavior (no recent-turn fallback).
        self.recent_context = recent_context
        # Optional ANTICIPATION ENGINE (see core/anticipation.py). Only consulted when BOTH an
        # Anticipator is wired here AND ``cfg.anticipation_enabled`` is True; otherwise the run is
        # byte-for-byte identical (zero calls, zero threads, zero events). Never raises: every
        # touch point is guarded so any failure degrades to the normal path.
        self.anticipator = anticipator
        # DIRECT TOOLS (core/tools.py): when a registry with at least one tool is given, the
        # planner gains the "tool" action and sees the tools relevant to each request; deep
        # briefs list the same tools with the shell command that calls them. None/empty = the
        # loop is exactly what it was before tools existed.
        self.tools: Optional[ToolRegistry] = tools if tools else None
        # How much the code adds around a deep goal beyond the standing instructions (``core/goal_iteration.py``,
        # solution iterate-by-spec): the slice of the specification in the brief, and the runner's own readings of
        # the metric before and after. The metric record every deep goal keeps (``core/goal_metrics.py``) runs under
        # every arm; this only says how much of it the code fills in itself. None = the selected arm, the same default
        # ``build_orchestrator`` uses, so a consumer that builds the brain itself (quest-backend does) records the
        # metric before and after by code too; it turns that off the way any selection is turned off.
        self.goal_iteration: Any = goal_iteration if goal_iteration is not None else resolve("iterate-by-spec")()
        # Optional LIVE WEB adapter (the ``WebResearch`` interface, ``core/adapters_retrieval.py``). When
        # wired, the planner gains its own two read keys -- {"web": "<query>"} and
        # {"web_page": "<url>", "focus": "<...>"} -- dispatched directly in ``_exec_one_read``,
        # NEVER broadcast through ``self.retrieval``/``CompositeRetrievalAdapter`` (that composite
        # fans every grep/query out to every member adapter, which would fire a paid, slow web
        # search on an ordinary corpus read). None (the default) means exactly today's behavior:
        # no WEB block in the planner prompt, no web/web_page read keys in the decide schema, and
        # a stray {"web": ...} spec from a model that hallucinated one returns a named "not
        # configured" error instead of being silently dropped.
        self.web: Optional[Any] = web
        # Operations learned from earlier turns, replayed before any context search (core/recipes.py).
        self.recipes: Optional[RecipeStore] = recipes
        # Injected by ``config.build_orchestrator`` (core imports neither adapters nor config).
        # ``edited_files_card_updater(deep_result, context_meta, *, project_path, session_id,
        # model_provider, registry)`` folds a deep run's edited files into the cards it used; None
        # = that update is skipped. ``background_thread_registrar(thread, owner)`` hands an owned
        # background thread to the shutdown registry; None = the thread is simply not registered.
        self.edited_files_card_updater = edited_files_card_updater
        self.background_thread_registrar = background_thread_registrar
        # The single-flight handle for the turn-end anticipation learn/plan thread (see
        # _kickoff_anticipation): while it is alive, further kickoffs are skipped, not queued.
        self._anticipation_thread: Optional[threading.Thread] = None

    def get_provider_for_model(self, model: str) -> ModelProvider:
        """Auto-detect and return the provider for a given model based on name prefix.

        Intelligently routes to the right provider for multi-provider setups:
        - claude-* → Anthropic provider
        - gemini-* or models/* → Gemini provider (models/* is Gemini's convention)
        - gpt-* → OpenAI provider
        - deepseek* → DeepSeek provider (only when DEEPSEEK_API_KEY is set)
        Falls back to primary provider if no multi-provider setup or no match.
        """
        if not model:
            return self.provider

        providers = getattr(self.registry, "_providers", {}) or {}
        wanted = provider_name_for_model(model)
        if wanted and wanted in providers:
            log.debug(f"Routing model '{model}' to the {wanted} provider")
            return providers[wanted]
        if wanted:
            log.debug(f"Model '{model}' names the {wanted} provider, which is not registered. "
                      f"Available: {list(providers.keys())}")

        # Fallback to primary provider
        log.debug(f"Model '{model}' falling back to primary provider ({type(self.provider).__name__})")
        return self.provider
