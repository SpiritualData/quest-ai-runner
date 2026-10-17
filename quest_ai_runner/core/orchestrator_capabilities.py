"""Capability probes for optional runner and provider arguments, timeouts, and read-spec helpers.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

import inspect
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from .adapters import Observation
from .runtime_settings import core_settings


# ---------------------------------------------------------------------------
# The Orchestrator.
# ---------------------------------------------------------------------------

def _run_goal_accepts_emit(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts an ``emit`` keyword (directly or via **kwargs).

    Lets the orchestrator stream EXECUTION-lifecycle events to runners that opt in, while leaving
    older ``run_goal(*, goal, brief, model, max_turns)`` signatures untouched. Decided by signature
    inspection rather than a try/except so a runner with a side effect is never invoked twice.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "emit" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_context_preamble(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``context_preamble`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``: a per-task context preamble (e.g. an AI
    rep's pulled persona) is forwarded ONLY to runners that accept the kwarg, so older
    ``run_goal`` signatures keep working unchanged. Signature inspection, never try/except, so a
    runner with a side effect is never invoked twice.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "context_preamble" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def run_goal_accepts_gathered_observations(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``gathered_observations`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``. ``context_preamble`` hands a runner the
    brain's own reads for this turn already FLATTENED to text (see ``_render_gathered``), which
    mixes real data reads with conversation-history hits, card content, and anything else the
    gather step touched -- fine for grounding a code generator, but unsafe for a consumer that
    needs to tell "a real read over the person's own records" apart from everything else without
    re-parsing prose. ``gathered_observations`` is the SAME filtered list (``_brain_content``: this
    turn's gather, minus discovery/menu observations) BEFORE it is rendered to text -- each item
    the plain ``{"kind", "locator", "rel_path", "text", ...}`` shape ``Observation.to_dict()``
    produces -- so a runner that opts in can apply its OWN structural filter (kind/locator) over
    real dicts instead of scanning a flattened string. Forwarded ONLY to runners that accept the
    kwarg, so older ``run_goal`` signatures keep working unchanged.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "gathered_observations" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_run_id(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``run_id`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``. When accepted, the orchestrator passes
    the subgoal's own stable ``task_uuid`` as ``run_id`` so every retry (each of which may spawn a
    brand-new underlying process/session) tags its EVENT_EXEC events with the SAME id — otherwise
    a consumer's dashboard renders each retry as a new, duplicate deep-run entry for what is really
    one ongoing subgoal.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "run_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_working_dir(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``working_dir`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``: a per-task working-directory override
    (e.g. a quest's synced folder, so the deep agent starts where that quest's real work lives —
    see ``quest_autopilot_design.md``'s execution-environment section) is forwarded ONLY to a
    runner that accepts it, leaving older ``run_goal`` signatures untouched.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "working_dir" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_resume_session_id(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``resume_session_id`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``. When accepted, an attempt that ran out of
    its TURN BUDGET can be CONTINUED in the worker's own session rather than re-run from a cold
    start. A runner without the kwarg keeps today's behaviour (a fresh run each attempt).
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "resume_session_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def run_goal_accepts_is_subgoal(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts an ``is_subgoal`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_emit``. True ONLY on a fan-out (``multi``: the
    turn split into several concurrent subgoals), so a runner that opts in can scope ITSELF to
    just the one subgoal it was handed instead of re-deriving a sibling's work from the fuller
    USER'S REQUEST header every call also carries. Round-2 trace: a subtask's own code generation
    (QuestCommandRunner) read the whole turn's request as context and re-created a SIBLING
    subtask's goal alongside its own. A runner that ignores the kwarg, or a single-goal turn,
    behaves exactly as before -- this is purely additive.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "is_subgoal" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def run_goal_accepts_requester_user_id(deep_runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``requester_user_id`` keyword (or **kwargs).

    Same opt-in discipline as ``run_goal_accepts_is_subgoal``. A runner that accepts it is told which
    Quest user the work is for and hands it to the process it spawns as ``QAR_REQUESTER_USER_ID``
    (see ``core/requester.py``). A runner without the kwarg is called exactly as before.
    """
    try:
        sig = inspect.signature(deep_runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "requester_user_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def provider_call_accepts_layers(fn: Any) -> bool:
    """Whether a provider's ``plan``/``answer`` accepts the ``layers`` cache-hint kwarg (or **kwargs).

    Same opt-in discipline the deep-runner helpers use: the orchestrator passes ``layers`` ONLY to a
    provider whose method accepts it, so an older ModelProvider (or a test stub) written before the
    layered cache surface existed keeps working unchanged via its plain ``prompt``/``messages`` path.
    Decided by signature inspection rather than try/except so an LLM call with side effects is never
    issued twice.
    """
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    for p in sig.parameters.values():
        if p.name == "layers" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def provider_call_accepts_tier(fn: Any) -> bool:
    """Whether a provider's ``plan`` accepts an explicit ``tier``, for the cascade's review call.

    Some consumers resolve a call's model from their OWN tier config and ignore the model id the
    orchestrator hands them (a planner that "always runs cheap" is the common and correct shape).
    On such a provider the cascade could not reach a stronger model at all, so it asks for a TIER
    when the provider can take one, and falls back to the model id when it cannot. Signature
    inspection, same opt-in discipline as ``provider_call_accepts_layers``, so an older provider
    keeps working unchanged.
    """
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    for p in sig.parameters.values():
        if p.name == "tier" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


# --- WS1 reliability floor: configurable, loud timeouts (see docs/HANDS_FREE_QUEST_AI_DESIGN.md) --

def read_op_timeout_seconds() -> float:
    """Per-operation wall-clock budget for one mid-loop read/grep/query dispatched by
    ``Orchestrator._do_reads``. Env ``QAR_READ_OP_TIMEOUT_SECONDS`` (default 60, accepts a float);
    read fresh on every call, not cached, so a deployment (or a test) can change it without a
    restart. Before this existed, one slow retrieval adapter could wedge the whole turn with no
    signal at all; a timeout now reports plainly which operation stalled and for how long instead
    of silently looking like an empty "nothing found" result."""
    return core_settings().read_op_timeout_seconds


def quest_judge_timeout_seconds() -> float:
    """Wall-clock budget for the quest-selection judgment. Env ``QAR_QUEST_JUDGE_TIMEOUT_SECONDS``
    (default 8). On a timeout the turn simply goes without a guessed quest."""
    return core_settings().quest_judge_timeout_seconds


def intent_judge_timeout_seconds() -> float:
    """Wall-clock budget for the ONE structured intent-directive judgment call (see
    ``Orchestrator.judge_execution_directive``). Env ``QAR_INTENT_JUDGE_TIMEOUT_SECONDS`` (default
    8, accepts a float); read fresh on every call, not cached. A short cap is deliberate: this call
    only runs when the planner gave no ``user_intent`` verdict, and must never be the reason a turn
    feels slow -- a timeout falls back to "no directive" instead of blocking."""
    return core_settings().intent_judge_timeout_seconds


def mode_release_timeout_seconds() -> float:
    """Wall-clock budget for the ONE structured brainstorm-release judgment (see
    ``Orchestrator.judge_brainstorm_release``). Env ``QAR_MODE_RELEASE_TIMEOUT_SECONDS`` (default 8,
    accepts a float); read fresh on every call, not cached. A timeout HOLDS the latch (the fail-safe
    direction), so a slow provider can delay a brainstorm turn by at most this budget and can never
    cause work to run in a conversation the user put on hold."""
    return core_settings().mode_release_timeout_seconds


def describe_read_spec(spec: Dict[str, Any]) -> str:
    """Human-readable operation name for a read spec, mirroring the ``locator`` naming
    ``Orchestrator._exec_one_read`` gives its own Observations. Used only to name WHICH
    operation timed out, so a stall is reported as a named, diagnosable failure rather than an
    empty result."""
    if not isinstance(spec, dict):
        return "read"
    if spec.get("list_guidance"):
        return "list_guidance"
    if spec.get("read_guidance"):
        return f"read_guidance({spec['read_guidance']})"
    if spec.get("list_sources"):
        return "list_sources"
    if spec.get("describe_source"):
        return f"describe_source({spec['describe_source']})"
    if spec.get("list_operations"):
        return "list_operations"
    if spec.get("describe_operation"):
        return f"describe_operation({spec['describe_operation']})"
    if spec.get("cards") is not None:
        return f"cards({spec['cards']!r})"
    if spec.get("card") is not None:
        return f"card({spec['card']})"
    if spec.get("web") is not None:
        return f"web({spec['web']!r})"
    if spec.get("web_page"):
        extra = ""
        if spec.get("full") or spec.get("part"):
            extra = f", full, part {spec.get('part') or 1}"
        return f"web_page({spec['web_page']!r}{extra})"
    if spec.get("grep"):
        return f"grep({spec['grep']!r})"
    if spec.get("query") is not None:
        query = spec.get("query")
        text = query.get("text") if isinstance(query, dict) else query
        return f"query({text!r})" if text else "query"
    if spec.get("rel_path"):
        return f"read_section({spec['rel_path']})"
    return "read"


# REPEATED READS (found in live turns, 2026-10-06): a planner on a cheap OR a mid model re-issued
# the SAME read spec up to fourteen times in one turn when its result did not hold what it hoped,
# paying a read and a planning call each time and burying the gathered context in copies. The test
# is structural, on the planner's own read specs (never on words it wrote): the same keys and
# values, in any order, already ran this turn. Such a read is not run again; the planner is told
# so, and a second step that asks for nothing new answers from what was gathered.
def read_spec_key(spec: Dict[str, Any]) -> str:
    """Canonical identity of a read spec: the same spec in any key order is the same read."""
    try:
        return json.dumps(spec, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(spec)


def split_repeated_reads(reads: Optional[List[Any]], executed: Dict[str, int]
                         ) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], int]]]:
    """``(fresh, repeated)``: specs to run now, and specs that already ran this turn paired with
    the step they ran at. A spec listed twice in one step runs once."""
    fresh: List[Dict[str, Any]] = []
    repeated: List[Tuple[Dict[str, Any], int]] = []
    now: set = set()
    for spec in reads or []:
        if not isinstance(spec, dict):
            continue
        key = read_spec_key(spec)
        if key in executed:
            repeated.append((spec, executed[key]))
        elif key not in now:
            now.add(key)
            fresh.append(spec)
    return fresh, repeated


def planner_note_observation(text: str, locator: str) -> Dict[str, Any]:
    """A ``planner_only`` gathered entry: a remark for the planner about the turn itself. It is
    rendered after the planner's reads, never counted as something gathered, and never grounds an
    answer, a deep brief, or the turn's returned ``gathered``."""
    note = Observation(kind="query", locator=locator, text=text).to_dict()
    note["planner_only"] = True
    return note


def repeated_read_observation(spec: Dict[str, Any], step: int) -> Dict[str, Any]:
    """The gathered note that stands in for a read that was not run again."""
    return planner_note_observation(
        f"NOT RUN AGAIN: {describe_read_spec(spec)} with these exact arguments already ran at "
        f"step {step} of this turn, and its result is shown in full in what you gathered above. "
        "The same read returns the same result. Use that result, read something DIFFERENT, or "
        "choose another action.", "repeated_read")


def planner_full_view_start(content_count: int, recent_full: int, compress_over: int) -> int:
    """Index (among real observations) from which the per-step planner sees reads IN FULL; older
    ones are one-line summaries there (``_render_gathered_for_planner``). A read whose result is
    only a summary now may be run again: refusing it would leave the planner unable to see it."""
    recent_full = max(0, recent_full)
    if content_count <= compress_over or content_count <= recent_full:
        return 0
    return content_count - recent_full


def context_assembly_timeout_seconds() -> float:
    """Wall-clock budget for the turn-start context-assembly background fetch (cards + recent +
    corpus consolidation, run concurrently with the instant ack). Env
    ``QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS`` (default 30.0, accepts a float); read fresh on every
    call so it can be tuned without a restart. A soft deadline slightly under this budget is
    threaded to the assembler via ``meta["assembly_deadline"]`` so a deadline-aware assembler
    (e.g. ``HybridContextAssembler``) returns whatever completed in time as a PARTIAL result
    (``AssembledContext.partial``) instead of blowing the whole budget. Only when not even a
    partial result lands in time does the collect below drop ALL fresh context for the turn --
    see the WARNING logged at the call site plus ``record_context_assembly_timeout`` for the
    counter this triggers.

    The default was raised from 5.0 to 15.0 (2026-09) after measuring real assembly latency on a
    live deployment: a warm assembler (card store + vector search + one downstream profile fetch)
    typically completes in well under 1s, but a recurring weekly burst of concurrent deep-run
    subprocess spawns (a reliability-probe batch, one worker started roughly every minute for
    several minutes) reliably pushed assembly past the old 5.0s budget -- hitting the soft
    deadline a dozen times across two such bursts, and at least once blowing the hard budget
    entirely (0 cards / 0 sources for that turn). 15.0 gives real headroom over that observed tail
    while staying tiny next to the overall per-turn answer budget (``QAR_ANSWER_TIMEOUT``, minutes)
    and the deep-run wall-clock timeout (``QAR_DEEP_TIMEOUT_SECONDS``, an hour); the common case is
    unaffected since it already returns in under a second.

    Raised again 15.0 -> 30.0 (2026-09-26, Joshua: "better for things to work than to not"). The
    budget is a safety net, sized for a slow day rather than a typical one: dropping a turn's
    context costs far more than waiting a few more seconds for it, and the instant ack already
    tells the user the turn has started. Typical assembly is well inside it (1-11s measured on a
    5,800-card store after the 2026-09-26 turn-start cost fixes); a timeout still warns loudly."""
    return core_settings().context_assembly_timeout_seconds


def guidance_selection_timeout_seconds() -> float:
    """Wall-clock budget for the turn-start GuidanceProvider.select() call. Env
    ``QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS`` (default 15.0, accepts a float; raised from 5.0 on
    2026-09-26 for the same reason as ``context_assembly_timeout_seconds``); read fresh on every
    call so it can be tuned without a restart.

    ``GuidanceProvider.select()`` is a caller-supplied implementation (``core/adapters.py``'s
    ``GuidanceProviderBase``); a consumer's ``dynamic_guidance_loader`` or its own LLM filtering
    call inside ``select()`` can block on I/O with no timeout of its own. Called with no bound at
    all, that hangs the ENTIRE turn indefinitely, in the SAME "Searching context…" status the
    concurrent context-assembly fetch already shows — indistinguishable from that fetch stalling,
    but not actually protected by its timeout (context assembly runs in its own background thread
    collected with ``context_assembly_timeout_seconds()``; guidance selection was being called
    directly, synchronously, in the main turn thread, un-timed). See the call site in ``run()``."""
    return core_settings().guidance_selection_timeout_seconds


def verify_context_max_chars() -> int:
    """Character cap on the context layer handed to goal verification (``Orchestrator._verify_goal``).

    See HANDS_FREE_QUEST_AI_DESIGN.md sections 4 and 6: the overseer/verifier should affordably see
    the SAME context layer the worker/answer call saw (cache-read pricing makes that cheap once the
    layer is already cached), so ``_verify_goal`` is handed the turn's rendered L2 context block
    straight through, unchanged. This cap is a safety valve for the rare case where that block is
    itself huge (a card layer with no upstream cap), not a normal-path limiter. Env
    ``QAR_VERIFY_CONTEXT_MAX_CHARS`` (default 24000, accepts an int); read fresh on every call so it
    can be tuned without a restart. A non-positive value disables the cap entirely."""
    return core_settings().verify_context_max_chars


def truncate_verify_context(text: str, max_chars: Optional[int] = None) -> str:
    """Cap a context block for goal verification, dropping only its TAIL when it is too large.

    Truncating the tail (never the head, never by summarizing) keeps the returned block a true
    byte-PREFIX of the untruncated one -- the same stable-prefix property the whole layering scheme
    depends on (``prompt_layers.PromptLayers.prefix``). ``max_chars`` defaults to
    ``verify_context_max_chars()``; a non-positive cap means "no limit". Returns ``text`` unchanged
    when it is empty or already within the cap. When truncated, appends a short note naming how much
    was cut, so the judge knows the context it saw was partial rather than assuming completeness."""
    if not text:
        return ""
    cap = verify_context_max_chars() if max_chars is None else max_chars
    if cap <= 0 or len(text) <= cap:
        return text
    dropped = len(text) - cap
    head = text[:cap].rstrip()
    return (head + f"\n\n[... context truncated for verification: {dropped} more "
            "characters omitted, earliest content kept ...]")


context_assembly_timeout_lock = threading.Lock()


context_assembly_timeout_count = 0


def record_context_assembly_timeout() -> int:
    """Thread-safe increment of the process-wide context-assembly-timeout counter (module-level
    because assembly runs on a background executor thread per turn, and turns run concurrently).
    No metrics backend is wired in this repo, so this is the observable marker a consumer can
    poll/export; it is paired with a WARNING log at the call site, never silent. Returns the new
    total count."""
    global context_assembly_timeout_count
    with context_assembly_timeout_lock:
        context_assembly_timeout_count += 1
        return context_assembly_timeout_count
