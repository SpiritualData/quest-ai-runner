"""The planner's decision tool schema and the prompt wrappers that pick which variant a run uses.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from .planner_prompt import (
    DEFAULT_PLANNER_PROMPT_PROFILE,
    DEFERRED_DEEP_INLINE_SEMANTICS,
    MODEL_TIER_BLOCK,
    PLANNER_PROFILES,
    PLANNER_PROMPT,
    _RATIONALE_INSTRUCTION_PLAIN,
)
from .orchestrator_types import OrchestratorConfig


def planner_prompt_for_profile(profile: Optional[str]) -> str:
    """PUBLIC: the planner prompt for ``profile``, falling back to the full one.

    An unknown name degrades to "full" rather than raising: a planner prompt is not the place for
    a deployment to fail closed on a typo in an environment variable.
    """
    return PLANNER_PROFILES.get((profile or "").strip().lower() or
                                DEFAULT_PLANNER_PROMPT_PROFILE, PLANNER_PROMPT)


def planner_prompt_defaults() -> Dict[str, Any]:
    """PUBLIC: a safe default for EVERY ``PLANNER_PROMPT`` format slot.

    ``PLANNER_PROMPT`` is a public export, and every slot it carries is mandatory for a raw
    ``.format()`` call: adding one (``deferred_deep_semantics``, for the queued-deferred wording)
    breaks any external consumer that renders the prompt itself. This is the non-breaking path:
    pair it with ``render_planner_prompt``, which fills whatever the caller does not pass, so a
    future slot can be added without breaking anyone again.
    """
    cfg = OrchestratorConfig()
    return {
        "user_message": "",
        "transcript": "(no prior messages)",
        "context_view": "(no context)",
        "gathered": "(nothing gathered yet)",
        "max_reads": cfg.max_reads_per_step,
        "max_subq": cfg.max_subquestions,
        "max_deep": cfg.max_deep_subtasks,
        "mode_signal_block": "",
        "card_thread_block": "",
        # Inline is the DEFAULT deployment (OrchestratorConfig.deferred_deep_queued = False), so
        # the default wording must be the inline one: it is the only one true by default.
        "deferred_deep_semantics": DEFERRED_DEEP_INLINE_SEMANTICS,
        # Fail OPEN: with no runner known (true of a raw render with nothing wired), keep the
        # doctrine rather than silently drop it. ``Orchestrator._plan`` computes the real,
        # turn-aware value instead of using this default.
        "model_tier_block": MODEL_TIER_BLOCK,
        "rationale_instruction": _RATIONALE_INSTRUCTION_PLAIN,
    }


def render_planner_prompt(**slots: Any) -> str:
    """PUBLIC: render ``PLANNER_PROMPT`` with defaults for every slot the caller omits.

    ``render_planner_prompt(user_message="...")`` is the stable way for an external consumer to
    render the planner prompt. Pass any subset of the slots (see ``planner_prompt_defaults``);
    the rest are filled with their defaults instead of raising ``KeyError``.
    """
    values = planner_prompt_defaults()
    values.update(slots)
    return PLANNER_PROMPT.format(**values)


# The planner's structured reading of what the user's CURRENT message asks of the assistant
# (``PlanDecision.user_intent``). This REPLACED a regex net over the user's words (2026-10-07):
# every fix to that net was one more pattern, and each pattern leaked the next phrasing nobody had
# anticipated ("I'll lean on the claim ... and move on" queued a task because "move" is a change
# verb). The planner already reads the whole message, the transcript and the context to choose
# its action, so it states this verdict on the same call and the escalation nets honor it.
USER_INTENT_ACT = "act"


USER_INTENT_HOLD_OFF = "hold_off"


USER_INTENTS = (USER_INTENT_ACT, "ask", "inform", USER_INTENT_HOLD_OFF)


def normalize_user_intent(raw: Any) -> Optional[str]:
    """The planner's ``user_intent`` as one of ``USER_INTENTS``, or None when it is missing or not
    one of them (a hallucinated value is "not given", never a guess). Never raises."""
    if isinstance(raw, str) and raw.strip().lower() in USER_INTENTS:
        return raw.strip().lower()
    return None


# The structured decision schema the planner MUST return (forced tool use).
DECIDE_TOOL: Dict[str, Any] = {
    "name": "decide",
    "description": "Record the chosen NEXT step and its parameters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "answer", "deep", "confirm", "clarify"]},
            "reads": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string"},
                        "heading": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "grep": {"type": "string"},
                        "scope": {"type": "string"},
                        "query": {"type": "object"},
                        "time_range": {
                            "type": ["object", "null"],
                            "description": "Optional HARD filter alongside a query/grep: only "
                                           "consider content from this period. Resolve relative "
                                           "dates ('Wednesday', 'last week') to concrete dates.",
                            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                        },
                        "topic_terms": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Optional topic/keyword terms to narrow a query/grep.",
                        },
                        "actor": {"type": ["string", "null"], "enum": ["me", "rep", "team", None]},
                        "content_kind": {
                            "type": ["string", "null"],
                            "enum": ["tasks_done", "decisions", "conversations", "files", None],
                        },
                        "list_sources": {"type": "boolean"},
                        "describe_source": {"type": "string"},
                        "describe_path": {"type": "string"},
                        "list_operations": {"type": "boolean"},
                        "describe_operation": {"type": "string"},
                        "list_guidance": {"type": "boolean"},
                        "read_guidance": {"type": "string"},
                        "cards": {"type": "string",
                                  "description": "Fetch remembered topic/person/work context for "
                                                 "this query from the assistant's memory cards "
                                                 "(the SAME context loaded at turn start; callable "
                                                 "at any step). Use for accumulated knowledge, not "
                                                 "source-of-truth files (grep/read those)."},
                        "card": {"type": "string",
                                 "description": "Fetch ONE known context card's content by its id."},
                    },
                },
            },
            "goal": {"type": ["string", "null"]},
            "deep_brief": {"type": ["string", "null"]},
            "confirm_question": {"type": ["string", "null"]},
            "confirm_kind": {
                "type": ["string", "null"],
                "description": "Optional category for a confirm/clarify decision-request, "
                               "lowercase letters/digits/underscore/colon/hyphen only, max 64 "
                               "chars (e.g. 'approve', 'explicit:spend', 'notice:prod-ops'). "
                               "Omit for the default 'approve' (or 'clarify' when the user is "
                               "being asked to choose/provide input).",
            },
            "confirm_deadline": {
                "type": ["string", "null"],
                "description": "When this decision should be resolved by: an ISO datetime, or a "
                               "short relative form like 'in 48h' / 'in 2 days'. Omit for no "
                               "deadline.",
            },
            "confirm_review_url": {
                "type": ["string", "null"],
                "description": "Optional full http(s) URL the person should open to answer: the "
                               "draft, preview, doc or PR the question is about. Put it here, not "
                               "only inside the question text, so the app shows it as a prominent "
                               "tappable link. Omit when there is nothing to look at.",
            },
            "confirm_default_on_silence": {
                "type": ["string", "null"],
                "enum": ["hold", "proceed", None],
                "description": "What happens if nobody answers by the deadline: 'proceed' lets "
                               "the decision auto-resolve and the work continue; 'hold' (the "
                               "default) leaves it paused until a human answers.",
            },
            "model_tier": {"type": ["string", "null"], "enum": ["haiku", "sonnet", "opus", None]},
            "deep_difficulty": {
                "type": ["string", "null"],
                "enum": ["simple", "normal", "hard", None],
                "description": "When action='deep' or deferred_deep is set: how demanding the "
                               "work is. 'simple' only for clearly trivial mechanical work; "
                               "'normal' when unsure. Null otherwise.",
            },
            "deep_difficulty_reason": {
                "type": ["string", "null"],
                "description": "One short clause explaining deep_difficulty.",
            },
            "deep_target": {
                "type": ["string", "null"],
                "enum": ["quest_data", "code_or_files", "other", None],
                "description": "When action='deep' or deferred_deep is set: WHAT the change "
                               "lands on. 'quest_data' = the person's own Quest records and "
                               "fields (the quest's outcome or vision statement, measurable "
                               "outcomes, acceptance criteria, current state, preferences, "
                               "goals, habits, collection entries): these change through "
                               "Quest's own field-update operation or tool, NEVER by writing "
                               "code or editing files. 'code_or_files' = a change to code, "
                               "config or files in a repo or corpus. 'other' or null when "
                               "neither fits.",
            },
            "subquestions": {"type": "array", "items": {"type": "string"}},
            "deep_subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"goal": {"type": "string"}, "brief": {"type": "string"}},
                },
            },
            "deferred_deep": {
                "type": ["object", "null"],
                "description": "When action='answer', optionally specify deep work that runs "
                               "immediately after the answer, in the same turn (the answer ships "
                               "first, then this work executes right away).",
                "properties": {
                    "goal": {"type": "string", "description": "The goal for the follow-up deep work"},
                    "brief": {"type": "string", "description": "Brief for the follow-up deep work (optional, defaults to user message)"},
                    "rationale": {"type": "string", "description": "Why this deep work runs after the answer"},
                },
                "required": ["goal"],
            },
            "answer_contains_work_to_execute": {
                "type": "boolean",
                "description": "Set to true if this answer describes work the AI should execute (instead of just reporting). Triggers auto-escalation to deep.",
            },
            "clarification": {
                "type": ["object", "null"],
                "description": "When action='clarify', specify what user input/approval is needed.",
                "properties": {
                    "question": {"type": "string", "description": "What to ask the user"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "List of possible responses (if multiple-choice)"},
                    "allow_free_input": {"type": "boolean", "description": "Whether user can provide free-text input beyond options"},
                },
                "required": ["question"],
            },
            "rationale": {"type": "string"},
            # What the CURRENT message asks of the assistant (see USER_INTENTS). No description:
            # the rubric at the top of both planner profiles states it, so neither pays twice.
            "user_intent": {"type": "string", "enum": list(USER_INTENTS)},
            # Whether an "answer" to an "act" message declines the act on the records read. The
            # rubric at the top of both planner profiles states it; optional, absent means false.
            "act_declined": {"type": "boolean"},
            # The reads this decision still owes: what the planner itself says it has NOT yet
            # opened. Honored structurally by the loop (see ``PlanDecision.pending_read``). The
            # rubric at the top of both planner profiles states it, so the schema stays bare.
            "pending_read": {"type": "array", "items": {"type": "object"}},
            # What the planner found in CONTEXT and GATHERED that bears on the request, one short
            # quote with its source each. Honored structurally (see ``PlanDecision.facts_read``):
            # appended to a deep brief, carried to the answer. The rubric states it, so the schema
            # stays bare.
            "facts_read": {"type": "array", "items": {"type": "string"}},
        },
        # REQUIRED, like card_thread: an optional field is one a model quietly omits, and every
        # omission costs an intent-judge call on an answer turn.
        "required": ["action", "rationale", "user_intent", "pending_read", "facts_read"],
    },
}


# The opt-in DECIDE_TOOL variant used when OrchestratorConfig.mode_signals_enabled is on: the
# base schema plus the `mode_signal` field. Kept as a separate schema (rather than a field the
# planner is told to ignore) so a consumer that never opted in exposes no mode vocabulary at
# all -- the planner cannot misfire a signal it cannot express.
_MODE_SIGNAL_TOOL_FIELD: Dict[str, Any] = {
    "type": ["string", "null"],
    "enum": ["enter_brainstorm", "exit_brainstorm", None],
    "description": "Set ONLY when the user EXPLICITLY asks to change the working "
                   "mode: 'enter_brainstorm' when they explicitly want to think out "
                   "loud with nothing acted on, 'exit_brainstorm' when they "
                   "explicitly ask to proceed/act on what was discussed. Otherwise "
                   "null. Judge intent; a topic shift or musing is never a signal.",
}


DECIDE_TOOL_WITH_MODE_SIGNAL: Dict[str, Any] = copy.deepcopy(DECIDE_TOOL)


DECIDE_TOOL_WITH_MODE_SIGNAL["input_schema"]["properties"]["mode_signal"] = _MODE_SIGNAL_TOOL_FIELD


# The opt-in `card_thread` field (OrchestratorConfig.card_thread_enabled): the ONE field per-idea
# threading costs. It rides the planning call the orchestrator already makes every turn, so topic
# assignment adds ZERO extra LLM calls. Same discipline as the mode signal: a consumer that never
# opted in exposes no thread vocabulary at all, so the planner cannot misfire a field it cannot
# express.
_CARD_THREAD_TOOL_FIELD: Dict[str, Any] = {
    "type": ["string", "null"],
    "description": "Which TOPIC (context card) this message belongs to. Exactly one of: "
                   "'continue' (the current topic, the usual answer), "
                   "'switch_to:<card_id>' (one of the KNOWN TOPICS listed in the prompt, using its "
                   "id verbatim), or 'new:<short label>' (a genuinely new idea; 2 to 5 words). "
                   "Never a mode change and never an action. When in doubt, 'continue'.",
}


# The deferred_deep field description for a QUEUED deployment (OrchestratorConfig.
# deferred_deep_queued): the consumer wired a deep runner that enqueues the work as a background
# task, so the schema must describe that mechanism, not the inline same-turn one. The inline
# description lives on DECIDE_TOOL itself (the default configuration).
DEFERRED_DEEP_FIELD_DESC_QUEUED = (
    "When action='answer', optionally specify deep work to hand to the background task queue "
    "(the answer ships first; the queued work then runs as its own background task and the user "
    "is told in this conversation when it finishes)."
)


# The status line that corrects an escalation's "searching further..." announcement when the
# escalation was not started or came back empty, so status lines stay true.
OWN_ESCALATION_KEPT_STATUS = "Nothing more to add from a deeper search, keeping this answer."


def deep_declined_background(result: Any) -> bool:
    """True when a self-initiated deep run started nothing: every goal came back
    ``declined_background`` (see ``Orchestrator._run_deep``'s ``self_initiated``)."""
    results = list(getattr(result, "deep_results", None) or [])
    return bool(results) and all(getattr(d, "declined_background", False) for d in results)


def own_escalation_adds_nothing(result: Any, exec_record: Any = None, facts_before: int = 0) -> bool:
    """True when one of the orchestrator's OWN escalations should not replace the answer it
    already has: it started nothing (``deep_declined_background``), or it came back EMPTY with
    nothing else to report (no output, no text, no decision filed, no usage limit, and no
    operation it ran succeeded: ``facts_before`` is how many facts ``exec_record`` held before
    the escalation started). Found 2026-10-07 in Quest's chat: a last-resort run came back empty and
    the turn ended with no reply, so the person got a generic "could you tell me more?" instead
    of the totals the answer had already worked out. A run that filed a decision, hit the usage
    limit or changed data still reports itself."""
    if deep_declined_background(result):
        return True
    results = list(getattr(result, "deep_results", None) or [])
    if any((getattr(d, "output", "") or "").strip() for d in results):
        return False
    if (getattr(result, "text", "") or "").strip():
        return False
    if any(getattr(d, "decision_id", None) or getattr(d, "usage_limited", False) for d in results):
        return False
    facts = list(getattr(exec_record, "facts", None) or [])[facts_before:]
    if any(getattr(f, "succeeded", False) for f in facts):
        return False
    return True


# Reserved named-runner registry key for QUEUED deployments: when OrchestratorConfig.
# deferred_deep_queued is on and the consumer registered a runner under this key in
# ``deep_runners``, every planner ``deferred_deep`` is PINNED to that runner (bypassing the
# classifier) so deferred work always reaches the queue and is never re-routed to an inline
# runner. Without this key, deferred work resolves through the normal runner wiring.
#
# RESERVED, NOT CLASSIFIER-SELECTABLE: this key is reachable ONLY through the deferred hand-off's
# explicit runner_override. A ``deep_runner_classifier`` that returns it for an ordinary deep turn
# is REJECTED (the default runner handles that turn instead), because routing normal deep work to
# the queue runner would hand the goal loop a queue receipt and let it be reported as finished work.
DEFERRED_RUNNER_KEY = "deferred"


# The opt-in `tool_calls` field (the orchestrator was given a ToolRegistry, core/tools.py): the
# direct-tool action. Same discipline as the other opt-in fields: a consumer with no tools exposes
# neither the "tool" action nor this field, so the planner cannot choose a tool that isn't there.
TOOL_CALLS_TOOL_FIELD: Dict[str, Any] = {
    "type": "array",
    "description": "When action='tool': the tool calls to run now, in order, from the TOOLS "
                   "block. Each is {\"name\": <tool name>, \"args\": {<argument>: <value>}} "
                   "with real, final content in the args (never a placeholder). Ignored for any "
                   "other action: leave it empty unless action is 'tool'.",
    "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
        "required": ["name"],
    },
}


# The opt-in LIVE WEB read-spec fields (Orchestrator.web is wired, core/adapters_retrieval.WebResearch):
# added to ``reads.items.properties`` ONLY when configured, so a deployment with no web adapter
# pays zero schema tokens for fields it could never use. See the WEB block (PLANNER_WEB_HEAD)
# for the planner-facing usage rules; these are just the shapes the schema accepts.
_WEB_READ_SPEC_FIELDS: Dict[str, Dict[str, Any]] = {
    "web": {
        "type": ["string", "array"],
        "items": {"type": "string"},
        "description": "A live web search query, or a list of queries (run in parallel).",
    },
    "web_page": {
        "type": "string",
        "description": "Fetch ONE web page by its URL (from a prior web result) and extract "
                       "the relevant passages.",
    },
    "focus": {
        "type": "string",
        "description": "With web_page: what you need from that page.",
    },
    "full": {
        "type": "boolean",
        "description": "With web_page: read the whole document in order instead of excerpts.",
    },
    "part": {
        "type": "integer",
        "description": "With web_page and full: which part of a long document (1 is the first).",
    },
    "fresh": {
        "type": "boolean",
        "description": "With web/web_page: bypass the cache (only for facts that change by "
                       "the hour).",
    },
}


def strip_schema_descriptions(schema: Any) -> Any:
    """PUBLIC: a deep copy of ``schema`` with every ``description`` key removed.

    A schema's field descriptions are instruction, and on a provider that renders the schema into
    the prompt (every provider without native tool use) they are instruction the caller pays for
    on EVERY call. Measured 2026-10-05: the decide schema is 1,897 tokens as rendered, 1,192
    without its descriptions. Only worth doing when the prompt body already states the same thing,
    which is what the compact planner profile does, so this is reached through
    ``decide_tool_for(compact=True)`` rather than applied by default.
    """
    if isinstance(schema, dict):
        return {k: strip_schema_descriptions(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [strip_schema_descriptions(v) for v in schema]
    return schema


def decide_tool_for(mode_signals: bool, deferred_queued: bool,
                    card_thread: bool = False, tools: bool = False,
                    compact: bool = False, web: bool = False) -> Dict[str, Any]:
    """Return the decide-tool schema variant for this run's configuration.

    ``mode_signals`` adds the opt-in ``mode_signal`` field; ``card_thread`` adds the opt-in
    ``card_thread`` field (per-idea threading); ``deferred_queued`` swaps the ``deferred_deep``
    field description for the queued-background wording so the schema always tells the planner what
    the wired deep runner ACTUALLY does with deferred work. ``web`` adds the opt-in
    ``web``/``web_page``/``focus``/``fresh`` read-spec fields (``Orchestrator.web`` is wired) --
    same discipline as ``tools``: a deployment with no web adapter exposes no web vocabulary at
    all, so the planner cannot emit a read it could never execute. ``compact`` drops the field
    descriptions (see ``strip_schema_descriptions``), keeping the field names, types, enums and
    required list, which is the whole contract a response has to satisfy.
    """
    base = DECIDE_TOOL_WITH_MODE_SIGNAL if mode_signals else DECIDE_TOOL
    if not deferred_queued and not card_thread and not tools and not web:
        return compact_decide_schema(base) if compact else base
    tool = copy.deepcopy(base)
    if tools:
        props = tool["input_schema"]["properties"]
        props["action"]["enum"] = list(props["action"]["enum"]) + ["tool"]
        props["tool_calls"] = copy.deepcopy(TOOL_CALLS_TOOL_FIELD)
    if web:
        read_props = tool["input_schema"]["properties"]["reads"]["items"]["properties"]
        for name, field_schema in _WEB_READ_SPEC_FIELDS.items():
            read_props[name] = copy.deepcopy(field_schema)
    if deferred_queued:
        tool["input_schema"]["properties"]["deferred_deep"]["description"] = (
            DEFERRED_DEEP_FIELD_DESC_QUEUED)
    if card_thread:
        tool["input_schema"]["properties"]["card_thread"] = copy.deepcopy(_CARD_THREAD_TOOL_FIELD)
        # REQUIRED, not optional. An optional field is one a model quietly omits, and every omission
        # lands on the fail-safe ("continue the current card"), which reads as a topic that never
        # moves: a real run left three different ideas all sitting on the first card the user had
        # opened. Requiring it forces the judgment to actually happen every turn. The fail-safe is
        # unchanged and still catches a malformed or missing value; it just stops being the norm.
        required = tool["input_schema"].setdefault("required", [])
        if "card_thread" not in required:
            required.append("card_thread")
    return compact_decide_schema(tool) if compact else tool


#: Fields whose description survives the compact schema. Measured 2026-10-06 on the dev half
#: (gemini-2.5-flash-lite, reach judge on): with every description stripped, 14 of 38 hand-off
#: decisions wrote "I will hand this to the environment" as the answer and left `deferred_deep`
#: empty, so nothing ran. The field that carries the hand-off keeps saying what it is for.
COMPACT_SCHEMA_KEPT_DESCRIPTIONS = ("deferred_deep",)


def compact_decide_schema(tool: Dict[str, Any]) -> Dict[str, Any]:
    """The decide schema with field descriptions stripped, except ``COMPACT_SCHEMA_KEPT_DESCRIPTIONS``."""
    compact = strip_schema_descriptions(tool)
    full_props = (tool.get("input_schema") or {}).get("properties") or {}
    props = (compact.get("input_schema") or {}).get("properties") or {}
    for name in COMPACT_SCHEMA_KEPT_DESCRIPTIONS:
        desc = (full_props.get(name) or {}).get("description")
        if desc and name in props:
            props[name]["description"] = desc
    return compact
