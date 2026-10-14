"""Turning the planner's raw decision into a normalized one, plus the small judgments made on it.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from .adapters import (
    PlanDecision,
    WRITE_SURFACE_AGENT,
    WRITE_SURFACE_FILES,
    WRITE_SURFACE_OPERATIONS,
)
from .auxiliary_prompts import QUEST_DATA_WRITE_SURFACE_INSTRUCTION
from .model_registry import TIERS
from .deep_model_selection import normalize_difficulty
from .orchestrator_types import OrchestratorConfig
from .orchestrator_constants import log
from .orchestrator_decide_tool import USER_INTENT_ACT, normalize_user_intent


# ---------------------------------------------------------------------------
# Decision normalization (coerce a raw planner dict into a safe PlanDecision).
# ---------------------------------------------------------------------------

# Every key that names a read SURFACE in the planner's read vocabulary. A read dict carrying none of
# them is a structured lookup missing its "query" wrapper (see ``normalize_decision``).
READ_VOCABULARY_KEYS = frozenset({
    "grep", "rel_path", "query", "list_sources", "describe_source", "list_operations",
    "describe_operation", "list_guidance", "read_guidance", "cards", "card", "tools", "web",
    "web_page",
})


def normalize_decision(raw: Dict[str, Any], cfg: OrchestratorConfig, *,
                       tools_enabled: bool = False, web_enabled: bool = False) -> PlanDecision:
    # A provider's structured output is not guaranteed to be a dict: some models/SDKs return a LIST
    # (e.g. multiple tool calls, or a JSON array). Coerce to a dict so a stray shape degrades to a
    # safe "answer" instead of raising 'list' object has no attribute 'get' from the planner.
    if not isinstance(raw, dict):
        if isinstance(raw, list):
            raw = next((x for x in raw if isinstance(x, dict)), {})
        else:
            raw = {}
    action = (raw.get("action") or "answer").strip().lower()
    if action not in ("read", "answer", "deep", "confirm", "clarify") and not (
            action == "tool" and tools_enabled):
        action = "answer"

    tool_calls: List[Dict[str, Any]] = []
    if tools_enabled:
        for c in (raw.get("tool_calls") or []):
            if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip():
                args = c.get("args")
                tool_calls.append({"name": c["name"].strip(),
                                   "args": args if isinstance(args, dict) else {}})
    if action == "tool" and not tool_calls:
        # "tool" with nothing to call is not an action: answer from what was gathered.
        action = "answer"

    reads_in = raw.get("reads") or []
    clean_reads: List[Dict[str, Any]] = []
    if isinstance(reads_in, list):
        for r in reads_in[: cfg.max_reads_per_step]:
            if isinstance(r, dict) and (
                r.get("grep") or r.get("rel_path") or r.get("query")
                or r.get("list_sources") or r.get("describe_source")
                or r.get("list_operations") or r.get("describe_operation")
                or r.get("list_guidance") or r.get("read_guidance")
                or r.get("cards") or r.get("card")
                or (tools_enabled and r.get("tools"))
                or (web_enabled and (r.get("web") is not None or r.get("web_page")))
            ):
                clean_reads.append(r)
            elif isinstance(r, dict) and r and not (READ_VOCABULARY_KEYS & r.keys()):
                # A structured lookup written without the "query" wrapper (e.g. a named read
                # operation with its args, which a consumer's own discovery text may show
                # top-level) used to be DROPPED here, so a "read" step ran nothing and the turn
                # answered from no data (measured 2026-10-07: every read dropped on 7 of 10 eval
                # turns). It is the query adapter's job to run it or name what is wrong with it,
                # so it goes there whole. A spec using a surface this turn does not have (tools,
                # web) is still dropped above, never re-routed.
                clean_reads.append({"query": dict(r)})

    tier = raw.get("model_tier")
    if isinstance(tier, str):
        tier = tier.strip().lower()
        _tier_alias = {"haiku": "fast", "sonnet": "balanced", "opus": "quality"}
        if tier in _tier_alias:
            tier = _tier_alias[tier]
        if tier not in TIERS:
            tier = None
    else:
        tier = None

    subs: List[str] = []
    for s in (raw.get("subquestions") or []):
        if isinstance(s, str) and s.strip():
            subs.append(s.strip())
    subs = subs[: cfg.max_subquestions]

    deep_subs: List[Dict[str, Any]] = []
    for d in (raw.get("deep_subtasks") or []):
        if isinstance(d, dict) and (d.get("goal") or d.get("brief")):
            deep_subs.append({"goal": d.get("goal") or None, "brief": d.get("brief") or None})
    deep_subs = deep_subs[: cfg.max_deep_subtasks]

    deferred_deep: Optional[Dict[str, Any]] = None
    deferred_raw = raw.get("deferred_deep")
    if deferred_raw and isinstance(deferred_raw, dict) and deferred_raw.get("goal"):
        deferred_deep = {
            "goal": deferred_raw.get("goal"),
            "brief": deferred_raw.get("brief"),
            "rationale": deferred_raw.get("rationale"),
        }

    clarification: Optional[Dict[str, Any]] = None
    clarif_raw = raw.get("clarification")
    if clarif_raw and isinstance(clarif_raw, dict) and clarif_raw.get("question"):
        clarification = {
            "question": clarif_raw.get("question"),
            "options": clarif_raw.get("options") or [],
            "allow_free_input": bool(clarif_raw.get("allow_free_input", False)),
        }

    # Mode signal: only parsed when the consumer opted into planner-detected mode signals
    # (``cfg.mode_signals_enabled``); with the flag off a stray ``mode_signal`` in the response
    # is ignored outright. When on, strictly validated -- anything but the two known values
    # (wrong type, empty, garbage, a hallucinated mode name) normalizes to None, so a detection
    # failure can never change the consumer's mode (fail-safe).
    mode_signal = raw.get("mode_signal") if cfg.mode_signals_enabled else None
    if not (isinstance(mode_signal, str)
            and mode_signal.strip().lower() in ("enter_brainstorm", "exit_brainstorm")):
        mode_signal = None
    else:
        mode_signal = mode_signal.strip().lower()

    # Deep target: what a "deep" (or deferred_deep) change lands on. Same fail-safe shape as
    # deep_difficulty/mode_signal above -- anything that is not exactly one of the three enum
    # values (wrong type, empty, garbage, a hallucinated word) normalizes to None, so a detection
    # failure never changes ladder routing.
    deep_target_raw = raw.get("deep_target")
    if isinstance(deep_target_raw, str) and deep_target_raw.strip().lower() in (
            "quest_data", "code_or_files", "other"):
        deep_target = deep_target_raw.strip().lower()
    else:
        deep_target = None

    # Card thread (per-idea threading): only read when the consumer opted in. Kept RAW here (the
    # string the planner emitted); ``core.card_thread.parse_card_thread`` resolves and fail-safes it
    # in run(), where the candidate ids that make an id "known" are in hand. A non-string is dropped
    # to None, which resolves to "continue the current card".
    card_thread_raw = raw.get("card_thread") if cfg.card_thread_enabled else None
    if not (isinstance(card_thread_raw, str) and card_thread_raw.strip()):
        card_thread_raw = None
    else:
        card_thread_raw = card_thread_raw.strip()

    # confirm/clarify escalation extras: all optional, all normalized to None on anything that
    # is not a plain non-empty string (a hallucinated type, empty string) -- the fail-safe is
    # identical to every other optional planner field above: a bad value degrades to "not given",
    # never raises or gets forwarded as garbage to the escalation sink.
    confirm_kind_raw = raw.get("confirm_kind")
    confirm_kind_raw = confirm_kind_raw.strip() if isinstance(confirm_kind_raw, str) and confirm_kind_raw.strip() else None
    confirm_deadline_raw = raw.get("confirm_deadline")
    confirm_deadline_raw = confirm_deadline_raw.strip() if isinstance(confirm_deadline_raw, str) and confirm_deadline_raw.strip() else None
    confirm_url_raw = raw.get("confirm_review_url")
    confirm_url_raw = (confirm_url_raw.strip() if isinstance(confirm_url_raw, str) else "")
    if not re.match(r"^https?://\S+$", confirm_url_raw):
        confirm_url_raw = None
    confirm_silence_raw = raw.get("confirm_default_on_silence")
    if not (isinstance(confirm_silence_raw, str) and confirm_silence_raw.strip().lower() in ("hold", "proceed")):
        confirm_silence_raw = None
    else:
        confirm_silence_raw = confirm_silence_raw.strip().lower()

    # Self-reported confidence in THIS decision. Read only when the consumer opted into the
    # cascade (the field is not even in the schema otherwise), and strictly validated: anything
    # that is not one of the three values normalizes to None, which the cascade reads as "not
    # assessed" and leaves alone. A detection failure must never escalate every decision.
    confidence_raw = raw.get("confidence") if cfg.planner_cascade else None
    if isinstance(confidence_raw, str) and confidence_raw.strip().lower() in (
            "high", "medium", "low"):
        confidence = confidence_raw.strip().lower()
    else:
        confidence = None

    return PlanDecision(
        action=action,
        reads=clean_reads,
        goal=raw.get("goal") or None,
        deep_brief=raw.get("deep_brief") or None,
        confirm_question=raw.get("confirm_question") or None,
        model_tier=tier,
        deep_difficulty=normalize_difficulty(raw.get("deep_difficulty")),
        deep_difficulty_reason=(str(raw.get("deep_difficulty_reason")).strip()[:300]
                                if isinstance(raw.get("deep_difficulty_reason"), str)
                                and raw.get("deep_difficulty_reason").strip() else None),
        deep_target=deep_target,
        subquestions=subs,
        deep_subtasks=deep_subs,
        rationale=(raw.get("rationale") or "").strip(),
        deferred_deep=deferred_deep,
        answer_contains_work_to_execute=bool(raw.get("answer_contains_work_to_execute", False)),
        clarification=clarification,
        mode_signal=mode_signal,
        card_thread=card_thread_raw,
        confirm_kind=confirm_kind_raw,
        confirm_deadline=confirm_deadline_raw,
        confirm_review_url=confirm_url_raw,
        confirm_default_on_silence=confirm_silence_raw,
        tool_calls=tool_calls,
        confidence=confidence,
        user_intent=normalize_user_intent(raw.get("user_intent")),
    )


# ---------------------------------------------------------------------------
# Rendering helpers (gathered observations -> prompt text / grounding block).
# ---------------------------------------------------------------------------

def _truncate_goal(goal: str, max_chars: int = 3900) -> str:
    """Truncate goal text to stay under Quest's 4000-char limit."""
    if len(goal) > max_chars:
        return goal[:max_chars] + " [truncated]"
    return goal


def _clarify_question_text(plan: "PlanDecision") -> str:
    """Render a planner ``clarify`` decision as ONE user-facing question (options folded in).

    Shared by the escalating path (``_run_clarify``, which parks it as a decision-request) and the
    brainstorm path (which is forbidden to escalate, so it puts the same question in the reply).
    """
    clarif = (plan.clarification or {}) if plan is not None else {}
    question = (clarif.get("question") or "").strip() or "Need your input to proceed"
    options = clarif.get("options") or []
    if options:
        question += "\n\nOptions:\n" + "\n".join(f"- {opt}" for opt in options)
        if clarif.get("allow_free_input", False):
            question += "\n\n(You can also provide custom input)"
    return question


# A decision's summary is stored by Quest as a goal CONDITION — a short, human-readable done-standard.
# Raw text (a verbose planner question, a deep brief, dumped gathered context) must NEVER be written
# there: it overflows Quest's 4000-char limit and reads as a wall of text instead of a decision ask.
# When a summary is long it is condensed by an LLM into a one or two sentence ask before escalation.
_CONCISE_DECISION_LIMIT = 600  # summaries longer than this are condensed before they reach Quest


# ---------------------------------------------------------------------------
# OUR OWN GOAL VERIFICATION (replaces Claude Code's /goal self-check).
# After a deep worker runs, the brain decides whether the done-standard is met — judged through the
# AI rep's lens (rep_preamble) and AGAINST the applicable GUIDANCE CARDS, which encode the quality
# standards the result must satisfy — and, when it is not met, what the next attempt should do. This
# is the primitive the goal loop iterates on.
# ---------------------------------------------------------------------------


# The worker output the judge reads. A real deliverable (a multi-section brief, a report) runs well
# past a few thousand characters, and a judge handed only the first slice saw it stop mid-sentence
# and failed the run as "cut off" when nothing had been cut off. So the cap is generous, and when
# it still bites the view keeps the HEAD and the TAIL (the ending is where completeness shows) and
# SAYS that the omission belongs to this view only, never to the worker's output.
VERIFY_OUTPUT_MAX_CHARS = 40000


def verify_output_view(output: Optional[str], max_chars: int = VERIFY_OUTPUT_MAX_CHARS) -> str:
    text = output or ""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    dropped = len(text) - head - tail
    return (text[:head]
            + f"\n\n[... {dropped} characters omitted from THIS VIEW ONLY, to keep the judging "
              "prompt small. The worker's real output is complete and continuous here; the gap "
              "is NOT a cut-off, truncation or missing content, so never judge the output "
              "incomplete because of it ...]\n\n"
            + text[-tail:])


# Gates ``_verify_goal``'s web-evidence precedence note (below) onto this turn's gathered reads: it
# fires ONLY when at least one observation came from the LIVE WEB adapter (core/adapters.WebResearch
# -- a search's ``rel_path`` is "web_search:<q>", a page fetch's ``locator`` starts "web extract: "),
# never for an ordinary corpus/grep/query read, so a deployment with no web adapter wired -- or a
# turn that never searched -- renders byte-identical to before this existed.
def _gathered_has_web_evidence(gathered: Optional[List[Dict[str, Any]]]) -> bool:
    for obs in (gathered or []):
        if not isinstance(obs, dict):
            continue
        if str(obs.get("rel_path") or "").startswith("web_search:"):
            return True
        if str(obs.get("locator") or "").startswith("web extract:"):
            return True
    return False


# Cap on the verifier's LIVE WEB evidence section. Web observations are already small by
# construction (a search is ~5 trimmed snippets plus a short summary, a page fetch is held to the
# adapter's page token budget), so this is a safety valve for a turn with many web reads, not a
# normal-path limiter; it keeps the uncached volatile tail of every verify retry bounded.
VERIFY_WEB_EVIDENCE_MAX_CHARS = 8000


# ---------------------------------------------------------------------------
# BRAINSTORM-RELEASE JUDGE: the ONE structured judgment that decides, on a LATCHED brainstorm turn,
# whether the user released the hold. It exists because that decision is too consequential to ride
# the planner call at the cheap planner tier: a cheap planner reads a bare subject-matter imperative
# ("create a goal called X and add it to my plan") as "the user is asking to proceed", releases the
# latch mid-turn, and the work executes in a conversation the user explicitly put on hold. Acting is
# not recoverable; holding is (one sentence from the user). So the exit gets its own call, at its own
# tier, whose fail-safe direction is HOLD. It runs at most ONCE per turn and ONLY while the latch is
# held, so normal turns cost exactly what they cost today. Generic and app-agnostic.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# ASYNC POST-DEEP CONTEXT-CARD UPDATER (prepare for the FUTURE after a deep run).
# Two centralized prompts: (1) the instruction appended to a deep process's brief so its output ends
# with a machine-parseable "future context" section, and (2) the updater prompt that turns the run
# into a STRUCTURED set of card edits. Both forbid em dashes (hard brand rule). Generic: no org names.
# ---------------------------------------------------------------------------







def ladder_for_quest_data(ladder: List[Any]) -> List[Any]:
    """Narrow a deep-runner ladder to rungs that can actually satisfy a QUEST-DATA change.

    Called only when the plan's ``deep_target == "quest_data"`` (the person's own Quest records
    and fields: outcome/vision statement, measurable outcomes, acceptance criteria, current
    state, preferences, goals, habits, collection entries).

    A rung whose ``write_surface`` is ``WRITE_SURFACE_FILES`` can only change files on disk, so it
    is dropped outright -- a file edit can never change quest data and must never stand in for
    one. If any ``WRITE_SURFACE_OPERATIONS`` rung remains after that, keep ONLY those: a quest
    field update goes through the governed operation, never through generated code. Otherwise,
    keep the remaining rungs (the full-agent worker) unchanged, so the goal can still be executed
    -- the caller is responsible for telling that worker, via the brief, which tool to use.

    ``write_surface`` is read with ``getattr(rung, "write_surface", WRITE_SURFACE_AGENT)`` so a
    rung (including ``None``, the "nothing wired" placeholder) that never declares it is treated
    as a full agent, i.e. unaffected by this guard.
    """
    kept = [r for r in ladder if getattr(r, "write_surface", WRITE_SURFACE_AGENT) != WRITE_SURFACE_FILES]
    operations_only = [r for r in kept if getattr(r, "write_surface", WRITE_SURFACE_AGENT) == WRITE_SURFACE_OPERATIONS]
    return operations_only if operations_only else kept


def apply_quest_data_ladder_guard(
    ladder: List[Any], brief: str, *, deliberate_choice: bool
) -> Tuple[List[Any], str]:
    """The quest-data guard for a deep goal whose ``deep_target == "quest_data"``.

    Returns ``(ladder, brief)``. A ``deliberate_choice`` ladder (a pinned ``runner_override`` or a
    classifier-selected named runner) is never re-routed, but if its rung is not the governed-
    operations rung the brief gains ``QUEST_DATA_WRITE_SURFACE_INSTRUCTION``. Otherwise the default
    ladder is narrowed by ``ladder_for_quest_data`` (file-editing rungs dropped, operations rungs
    preferred), and the instruction is appended when no operations rung survived.
    """
    def surface(r: Any) -> Any:
        return getattr(r, "write_surface", WRITE_SURFACE_AGENT)

    if deliberate_choice:
        rung = ladder[0] if ladder else None
        if surface(rung) != WRITE_SURFACE_OPERATIONS:
            brief = brief + QUEST_DATA_WRITE_SURFACE_INSTRUCTION
        return ladder, brief

    narrowed = ladder_for_quest_data(ladder)
    if narrowed != ladder:
        dropped = [r for r in ladder if surface(r) == WRITE_SURFACE_FILES]
        log.info(
            "Quest-data ladder guard: dropped %d file-editing rung(s) "
            "(a file edit cannot change quest data); narrowed ladder to %d "
            "rung(s) with write_surface=%s",
            len(dropped), len(narrowed), [surface(r) for r in narrowed],
        )
        ladder = narrowed
    if not ladder:
        # Every rung could only edit files (a file-editing rung wired with no worker behind it).
        # ``[None]`` is the ladder's own "nothing can execute this" placeholder, which _do_run
        # reports honestly; leaving it empty would raise on ``runner_ladder[-1]`` at the call site.
        log.info("Quest-data ladder guard: no rung can change quest data, so this goal has "
                 "nothing to execute it")
        ladder = [None]
    if not any(surface(r) == WRITE_SURFACE_OPERATIONS for r in ladder):
        # No governed-operations rung survived: the agent worker still runs the goal, but must be
        # told which tool to use instead of writing code.
        brief = brief + QUEST_DATA_WRITE_SURFACE_INSTRUCTION
    return ladder, brief


def _normalize_card_edits(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a card-updater LLM result into a list of edit dicts. Accepts every shape models
    actually return: a ``{"edits": [...]}`` object, a BARE list of edit objects, a single
    ``{"card_id": ...}`` edit, or a list that contains a wrapper object. Returns [] on anything else.
    Never raises."""
    try:
        if isinstance(raw, dict):
            if isinstance(raw.get("edits"), list):
                return [e for e in raw["edits"] if isinstance(e, dict)]
            return [raw] if raw.get("card_id") else []
        if isinstance(raw, list):
            out: List[Dict[str, Any]] = []
            for el in raw:
                if isinstance(el, dict):
                    if isinstance(el.get("edits"), list):
                        out.extend(e for e in el["edits"] if isinstance(e, dict))
                    elif el.get("card_id"):
                        out.append(el)
            return out
    except Exception:  # noqa: BLE001
        pass
    return []


def _proposed_card_text(edit: Dict[str, Any]) -> str:
    """Build the text that REPRESENTS a would-be-created card from a card-updater edit, for the
    semantic-merge similarity check. Mirrors ``card_repository.card_embed_text`` (the canonical card
    embed text) WITHOUT importing it, so the brain keeps no dependency on a concrete adapter module:
    the topic NAME and DESCRIPTION, then each ``add`` item's ``why`` and any ``note`` text. Returns
    "" when there is nothing to match on (then the caller skips the merge and creates as before).
    Never raises."""
    try:
        parts: List[str] = []
        for _k in ("name", "description"):
            v = edit.get(_k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for it in (edit.get("add") or []):
            if not isinstance(it, dict):
                continue
            why = it.get("why")
            if isinstance(why, str) and why.strip():
                parts.append(why.strip())
            if it.get("type") == "note":
                note_text = (it.get("locator") or {}).get("text")
                if isinstance(note_text, str) and note_text.strip():
                    parts.append(note_text.strip())
        return " ".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


# The keys/values ``parse_goal_condition_reply`` accepts from the OPTIONAL constraints line above.
# Anything else (an unrecognized key, or a value outside these enums) is DROPPED, not passed
# through opaquely -- a hallucinated field must never reach a store as a real filter.
_CONSTRAINT_ACTOR_VALUES = frozenset({"me", "rep", "team"})


_CONSTRAINT_CONTENT_KIND_VALUES = frozenset({"tasks_done", "decisions", "conversations", "files"})


def _format_now_block(now: Optional[str]) -> str:
    """Render the CURRENT DATE line fed to ``DERIVE_GOAL_CONDITION_PROMPT`` so relative date
    expressions ("Wednesday", "last week") resolve against a real "today". ``now`` is an optional
    ISO date/datetime string; absent or unparseable falls back to the system clock (UTC), so
    relative-date resolution is always possible and this never blocks the turn."""
    dt = None
    if now:
        try:
            dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    return f"CURRENT DATE: {dt.strftime('%Y-%m-%d')} ({dt.strftime('%A')})"


def parse_goal_condition_reply(raw: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Split a ``DERIVE_GOAL_CONDITION_PROMPT`` reply into ``(goal_condition, constraints)``.

    The reply is normally just the restated instruction (line 1) -- ``constraints`` is None and
    behavior is byte-for-byte what it was before constraints existed. The reply MAY carry a second
    line with a single JSON object naming OPTIONAL structured retrieval constraints (see the
    prompt): ``time_range`` ({{"start", "end"}}), ``topic_terms`` (list of strings), ``actor``
    ("me"|"rep"|"team"), ``content_kind`` ("tasks_done"|"decisions"|"conversations"|"files").

    Best-effort and NEVER raises: malformed/missing JSON, or a JSON object with no recognized
    keys, yields ``constraints=None``. Only whitelisted keys/values survive parsing; anything else
    is dropped so a hallucinated field can never reach a store as a real filter."""
    text = (raw or "").strip()
    if not text:
        return "", None
    lines = text.splitlines()
    goal_condition = lines[0].strip()
    constraints: Optional[Dict[str, Any]] = None
    rest = "\n".join(lines[1:]).strip()
    if rest:
        try:
            match = re.search(r"\{.*\}", rest, re.DOTALL)
            obj = json.loads(match.group(0)) if match else None
        except Exception:  # noqa: BLE001 -- malformed constraints must never break the turn
            obj = None
        if isinstance(obj, dict):
            cleaned: Dict[str, Any] = {}
            time_range = obj.get("time_range")
            if isinstance(time_range, dict):
                start = time_range.get("start")
                end = time_range.get("end")
                if isinstance(start, str) or isinstance(end, str):
                    cleaned["time_range"] = {
                        "start": start if isinstance(start, str) else None,
                        "end": end if isinstance(end, str) else None,
                    }
            topic_terms = obj.get("topic_terms")
            if isinstance(topic_terms, list):
                cleaned_terms = [str(t).strip() for t in topic_terms if str(t).strip()]
                if cleaned_terms:
                    cleaned["topic_terms"] = cleaned_terms
            actor = obj.get("actor")
            if isinstance(actor, str) and actor.strip().lower() in _CONSTRAINT_ACTOR_VALUES:
                cleaned["actor"] = actor.strip().lower()
            content_kind = obj.get("content_kind")
            if (isinstance(content_kind, str)
                    and content_kind.strip().lower() in _CONSTRAINT_CONTENT_KIND_VALUES):
                cleaned["content_kind"] = content_kind.strip().lower()
            if cleaned:
                constraints = cleaned
    return goal_condition, constraints


# A message that carries no request to resolve: a greeting, a thanks, an acknowledgement.
#
# These exist because of what a cheap model does with "Hello" when asked to restate it as a
# done-standard: it stops restating and starts HELPING ("Hello. How can I help you with your Quests
# today?"). That answer-shaped string then became the turn's goal condition, which put it on the
# understanding channel as "Understood as: Hello. How can I help you..." AND injected it into the
# answer's context as the UNDERSTOOD REQUEST. A greeting has nothing to resolve, so the fix is not to
# ask: skip the LLM hop entirely and let the message stand as its own goal condition. One less round
# trip on the most common turn there is, and the echo cannot be generated in the first place.
_SMALL_TALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|hiya|howdy|sup|greetings|good\s+(morning|afternoon|evening|day)|"
    r"thanks|thank\s+you|thx|ty|ok|okay|k|cool|nice|great|awesome|got\s+it|understood|"
    r"bye|goodbye|see\s+you|good\s?night)"
    r"[\s!.,?~]*(there|everyone|team|again|so\s+much|a\s+lot)?[\s!.,?~]*$",
    re.IGNORECASE,
)


def is_small_talk(user_message: str) -> bool:
    """True for a bare greeting / thanks / acknowledgement: a message with no request in it.

    Deliberately conservative. It matches only a SHORT message that is nothing but the pleasantry,
    so "hi, can you create a habit for me" (a real request that opens with a greeting) does NOT
    match and still gets a proper goal condition.
    """
    text = (user_message or "").strip()
    if not text or len(text) > 40:
        return False
    return bool(_SMALL_TALK_RE.match(text))


def restates_meaningfully(goal_condition: str, user_message: str) -> bool:
    """True when the resolved goal condition says something the raw message did not.

    The understanding event ("Understood as: ...") is internal, but a consumer may surface it in a
    details/debug view, so it should only fire when the resolution actually ADDS information. The
    old guard was a byte-for-byte ``!=``, so a cheap-tier restatement that merely re-punctuated or
    re-cased the message ("Hello" -> "Hello.") counted as a fresh understanding, and the person got
    their own word echoed back at them above the reply. Compare normalized text instead: same words
    in the same order means nothing was learned, so stay quiet.
    """
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()
    return norm(goal_condition) != norm(user_message)


# Pure acknowledgements / deferrals that carry NO standalone instruction — they only make sense
# against prior conversation. A whole message equal to one of these needs context to be understood.
_ACK_PHRASES = frozenset({
    "ok", "okay", "k", "yes", "yep", "yeah", "yup", "sure", "go ahead", "go for it",
    "do it", "please do", "do that", "do so", "continue", "proceed", "carry on",
    "same as before", "as before", "like before", "as we discussed", "as discussed",
    "sounds good", "go", "agreed", "approved", "confirm", "confirmed",
})


# Anaphora / reference words that point at something earlier in the conversation. A short message
# leaning on one of these WITHOUT a concrete noun cannot be understood on its own.
_ANAPHORA_RE = re.compile(
    r"\b(it|its|that|this|those|these|them|they|the first one|the second one|the last one|"
    r"the other one|the previous one|as we discussed|as discussed|like before|as before|"
    r"same as before)\b",
    re.IGNORECASE,
)


# A guidance card may state a model preference for its kind of work (e.g. a "model selection" card
# whose body contains "model: sonnet" or "preferred model: claude-opus-4-8"). This scans the
# applicable-guidance text for such a directive so the deep worker honors it. Returns None if absent.
_GUIDANCE_MODEL_RE = re.compile(
    r"(?:preferred[ _]+)?model\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]{1,60})", re.IGNORECASE)


def _guidance_model_pref(quality_standards: Optional[str]) -> Optional[str]:
    """Extract a model preference declared in the applicable guidance cards, or None. Never raises."""
    if not quality_standards:
        return None
    try:
        m = _GUIDANCE_MODEL_RE.search(quality_standards)
        if m:
            return m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# THE REGEX NET OVER THE USER'S WORDS IS RETIRED (2026-10-07). Whether an answer turn should
# escalate to work used to be decided here by keyword lists (change verbs, wrongness words,
# interrogative openers, polite-command openers, discourse markers, "I'll ..." plans, hold-off
# phrases). Every misroute was "fixed" with one more pattern, and each pattern leaked the next
# phrasing. The escalation nets now honor the planner's structured ``user_intent`` verdict (see
# USER_INTENTS), with the one-shot ``judge_execution_directive`` as the fallback when the verdict
# is missing. Do not reintroduce a keyword reading of the message for that decision: if the planner
# misreads intent, fix the planner prompt.
#
# What remains below is the minimum the PRE-PLANNER veto (``message_forbids_new_task``) needs. It
# has to run before any planner verdict exists, because it shapes the planner's own prompt and
# action space for the turn, and it can only ever REMOVE execution, never add it.
_CHANGE_VERBS = (
    r"fix|implement|build|refactor|add|remove|delete|change|update|edit|rewrite|apply|create|"
    r"make|migrate|rename|move|replace|configure|enable|disable|integrate|wire\s+up|hook\s+up|"
    r"set\s+up|ensure|adjust|correct|patch|resolve|handle|support|improve|optimize|"
    r"expand|collapse|toggle|show|hide|open|close|display|render|scroll|load|initialize|reset|"
    r"convert|transform|format|parse|extract|inject|wrap|unwrap|expose|attach|detach"
)


_CHANGE_VERB_RE = re.compile(r"\b(?:" + _CHANGE_VERBS + r")\b", re.IGNORECASE)


# Words that turn a change verb into a NOUN: "give me an update", "the latest change", "a quick
# fix", "any improvements". Without this, every status request read as an order to go change
# something, because "update"/"fix"/"change"/"report" are the same string as verb or noun.
_NOUN_DETERMINERS = frozenset((
    "a", "an", "the", "any", "some", "no", "this", "that", "these", "those", "another",
    "my", "your", "our", "his", "her", "their", "its", "one", "each", "every", "such",
))


_WORD_RE = re.compile(r"[A-Za-z'\u2019]+")


def _change_verb_used_as_verb(message: str) -> bool:
    """True when at least one change verb in ``message`` is used AS A VERB, not as a noun.

    A determiner in the two words before the match ("an update", "the latest change", "a quick
    fix") means the human is naming a THING, usually to ask about it, not ordering the action.
    "update the sheet" keeps its verb reading because nothing determiner-like precedes it.
    """
    try:
        for m in _CHANGE_VERB_RE.finditer(message):
            preceding = _WORD_RE.findall(message[:m.start()])[-2:]
            if any(w.lower() in _NOUN_DETERMINERS for w in preceding):
                continue
            return True
    except Exception:  # noqa: BLE001
        return bool(_CHANGE_VERB_RE.search(message))
    return False


# The PRE-PLANNER veto: the human talking about whether a TASK MAY BE OPENED AT ALL ("don't create
# a task", "no new tasks", "just answer me here", "hold off", "I haven't given you an instruction
# yet"). It vetoes a planner "deep" too, which is why it cannot wait for the planner's verdict.
#
# "kill/cancel/dismiss that run" is deliberately NOT included: that is an instruction to ACT on the
# runner's own state, and a consumer may well execute it as deep work. Suppressing the planner
# there would answer "sure, I'll cancel it" and cancel nothing. The escalation nets read that case
# from the planner's ``user_intent`` ("hold_off"), not from here.
_FORBIDS_NEW_TASK_RE = re.compile(
    r"(?:"
    r"\b(?:do\s*n[o’']?t|dont|never|stop|avoid|refrain\s+from|without)\b[^.!?\n]{0,40}"
    r"\b(?:creat\w*|open\w*|start\w*|spawn\w*|queu\w*|mak\w*|run\w*)\b[^.!?\n]{0,25}\btasks?\b"
    r"|\bno\s+(?:new\s+|more\s+)?tasks?\b"
    r"|\bjust\b[^.!?\n]{0,30}\b(?:answer|reply|respond|tell\s+me|drop|say)\b"
    r"|\b(?:answer|reply|respond|drop)\b[^.!?\n]{0,25}\b(?:here|in\s+(?:the\s+)?chat)\b"
    r"|\bhave\s*n[o’']?t\s+(?:even\s+)?(?:given|asked|told)\b|\bhavent\s+(?:even\s+)?(?:given|asked|told)\b"
    r")",
    re.IGNORECASE,
)


# "hold on", "hold off", "stand by", "not yet" -- unlike every phrase in _FORBIDS_NEW_TASK_RE above,
# these four name no TOPIC of their own (no "task", no "answer"), they just mean "pause". That makes
# them ambiguous in a mixed message: "hold off on the emails, but go ahead and update the leads
# sheet" and "the deploy is not yet done -- fix it" each contain one of these phrases while also
# containing a directive that should still execute. Kept separate so ``message_forbids_new_task``
# can require the rest of the message to carry no directive of its own before honoring a bare match
# as a turn-wide veto; the phrases in _FORBIDS_NEW_TASK_RE stay an unconditional veto since they are
# already about the assistant's own task-opening machinery, not just "wait".
_BARE_HOLD_PHRASE_RE = re.compile(r"\bhold\s+(?:on|off)\b|\bstand\s+by\b|\bnot\s+yet\b", re.IGNORECASE)


def message_forbids_new_task(message: Optional[str]) -> bool:
    """True when the human's own words say NO TASK MAY BE OPENED for this message.

    Unlike the planner's ``user_intent == "hold_off"`` (which only gates the escalation nets), this
    DOES veto a planner ``action="deep"``: it degrades the turn to "answer", through the same structural
    no-action gate brainstorm mode uses. Without it, "don't create a task, just answer me here"
    was unanswerable -- every guard in this file could only ever ADD execution, so the one thing a
    user could not do was ask for less of it, and the request not to open a task opened one.

    The bare phrases in ``_BARE_HOLD_PHRASE_RE`` ("hold on", "hold off", "stand by", "not yet") are
    honored only when nothing ELSE in the message is itself a directive (a change verb used as a
    verb) -- otherwise a mixed instruction like "hold off on the emails, but go ahead and update the
    leads sheet" or "the deploy is not yet done -- fix it" would answer instead of executing the
    part that was never held back: the same false-completion failure this file is full of fixes
    for, just in the other direction.

    Read from the USER's own message only, never from model output. Never raises.
    """
    if not message or not message.strip():
        return False
    try:
        if _FORBIDS_NEW_TASK_RE.search(message):
            return True
        if _BARE_HOLD_PHRASE_RE.search(message) and not _change_verb_used_as_verb(message):
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def user_ordered_work(user_intent: Optional[str]) -> bool:
    """True when the planner's structured verdict says the user's CURRENT message asks the
    assistant to do or change something now (``user_intent == "act"``). Missing is False here;
    callers that must not lose a real order to a missing verdict ask the intent judge instead."""
    return user_intent == USER_INTENT_ACT
