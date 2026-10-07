"""Overseer — a minimal-intervention watcher for the Orchestrator run loop.

The idea (product owner): a HIGH-QUALITY model, reading very few tokens and writing very few,
watches a run the way a human consciousness watches their own body walk. Most of the time it says
nothing; occasionally it sends ONE tiny signal that causes a large downstream course correction.

Modeled on ``core/guard.py``: self-contained, NEVER raises, owns its own prompt/tool constants, and
exposes a PURE digest builder so the caller can decide when to consult it. It knows nothing about
Quest, any org, or the orchestrator's internals beyond a compact digest string. Any internal failure
degrades to the safe default ``OverseerSignal("proceed")`` (do nothing, keep going as if the overseer
were off), so wiring it in can never change a run's outcome except through an explicit signal.

The five signals:
  - ``proceed``        — the run is on track; do nothing (the overwhelming default).
  - ``redirect``        — the plan is drifting off-subject or wasting reads; nudge with ONE hint.
  - ``answer_now``      — enough has been gathered; stop reading and answer.
  - ``escalate_deep``   — this genuinely needs real execution (a code/file change, running or
    committing work); hand off to deep execution. This is routine, AI-doable work, not a human fork.
  - ``escalate_human``  — this is a genuine HUMAN-ONLY fork (identity, an irreversible/authorization
    decision, or an ambiguity only the user/owner can resolve); hand off to a confirm/decision-
    request instead of guessing. Mirrors this org's "AI acts first, only genuine forks go to a
    human" principle, so it must NOT fire on routine automatable work.

The digest fed to the model is CHEAP: a compact snapshot capped at a char budget, with the last few
operations summarized to one line each (never their full bodies), plus token/time/read counters. This
keeps the overseer's own token cost tiny, which is the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .adapters import STEP_JUDGE, plan_with_step


# ===========================================================================
# The signal — the overseer's whole output.
# ===========================================================================

@dataclass
class OverseerSignal:
    """One overseer decision. ``proceed`` is the do-nothing default.

    ``hint`` is only meaningful for ``redirect``: a single short course correction (kept under
    ~200 chars). ``reason`` is a one-sentence, user-safe explanation of the signal (may be surfaced
    as the overseer event's text), and is empty for a plain proceed.

    ``degraded`` distinguishes a REAL "proceed" verdict from a failure that fell back to proceed
    (provider error, non-dict response, unrecognized signal). The run treats both identically (do
    nothing), but the caller can use it to retry the consult at a different tier: a deployment
    whose overseer tier resolves to a model the wired provider cannot serve would otherwise have a
    permanently silent overseer that looks exactly like a healthy one.
    """
    signal: str = "proceed"    # "proceed" | "redirect" | "answer_now" | "escalate_deep" | "escalate_human"
    hint: str = ""              # only for redirect: ONE short course correction
    reason: str = ""             # one sentence, user-safe
    degraded: bool = False       # True when this proceed is a failure fallback, not a verdict


_VALID_SIGNALS = ("proceed", "redirect", "answer_now", "escalate_deep", "escalate_human")


# ===========================================================================
# Structured tool + prompt — centralized module constants.
# ===========================================================================

OVERSEE_TOOL: Dict[str, Any] = {
    "name": "oversee",
    "description": "Emit a single minimal-intervention signal about the run's direction.",
    "input_schema": {
        "type": "object",
        "properties": {
            # Descriptions here are DELIBERATELY bare mechanical minimums, not semantics: the full
            # behavioral meaning of each signal/field lives ONLY in OVERSEER_PROMPT's prose (see the
            # signal list + Rules section there). ClaudeCliProvider.plan() appends this ENTIRE schema
            # as inline JSON text on EVERY consultation (it cannot force native tool_choice), so any
            # duplication here is a real, repeated token cost, not a one-time one.
            "signal": {
                "type": "string",
                "enum": list(_VALID_SIGNALS),
            },
            "hint": {
                "type": "string",
                "description": "only for redirect, under 200 chars",
            },
            "reason": {
                "type": "string",
                "description": "one short sentence",
            },
        },
        "required": ["signal"],
    },
}

OVERSEER_PROMPT = """\
You are a minimal-intervention OVERSEER watching an AI assistant work through a request, the way a
person's quiet awareness watches their own body walk: usually silent, occasionally sending one small
signal that changes course.

DIGEST fields below, and why each matters:
  - CURRENT USER REQUEST (+ RESOLVED AS): the user's literal words, plus what they were resolved to;
    judge against this actual request, not a sibling topic.
  - RECENT CONVERSATION / PRIOR ESCALATIONS: prior turns and any earlier escalation to deep work or
    a human; use both to catch drift and repeated escalation with no progress.
  - OPERATIONS THIS TURN: exactly what has been read or searched so far; use it to catch redundant
    or off-topic reads.
  - PASS / RATIONALE / CURRENT PLAN: which planning pass this is, the planner's own stated reason,
    and what it is about to do next; check the plan still serves the request and the rationale
    actually supports it.
  - SPEND / TIME / AGENT'S READ BUDGET: tokens, wall-clock, and read volume against their caps; none
    is a hard stop alone, but nearing a cap with no path to an answer is a signal to answer_now.
  - QUALITY BAR: the completion standard the result must clear; a draft that ignores it is not done.
  - DRAFT ANSWER: the proposed reply, only present at the final checkpoint; judge whether it truly
    satisfies the request and the quality bar.

Choose EXACTLY ONE signal via the tool:
  - "proceed": the run is on a reasonable track. DEFAULT. When unsure, proceed.
  - "redirect": the plan is clearly off-subject or wasting reads on material that will not answer
    the request. Give "hint": ONE short course correction (under 200 chars), not a plan.
  - "answer_now": enough has already been gathered to answer well; more reading is waste.
  - "escalate_deep": the REQUEST uses an action verb (add, fix, implement, change, create, run,
    commit, send, delete, refactor) AND is PHRASED AS AN INSTRUCTION to perform it (an imperative,
    or a polite command like "can you add X"), the plan is only reading or drafting an answer ABOUT
    the work instead of executing it, and this is ROUTINE, AI-doable work, not a human decision. Do
    NOT escalate_deep for a QUESTION that merely mentions an action verb while asking for
    information, an explanation or an opinion ("how would I add X?", "should we refactor Y?"): an
    interrogative opener (how/what/why/is/are/would/could/should we/do you/does it) asking ABOUT the
    work is a question even if it names an action, and the user wants an answer, not the change made.
  - "escalate_human": a genuine HUMAN-ONLY fork, not routine automatable work: an identity question,
    an irreversible or authorization-requiring action (an outward payment, a real-world commitment,
    deleting something unrecoverable), or a genuine ambiguity only the user/owner can resolve (a
    taste or direction call). Mirrors this org's "AI acts first" principle: must NOT fire just
    because a task is hard, unclear in a resolvable way, or needs more digging. When in doubt
    between escalate_deep and escalate_human, prefer escalate_deep; reserve escalate_human for a
    case an AI plainly should not decide or execute on its own. A request NOTHING here can carry out
    (a physical-world act, a system nothing here is connected to) is not a fork either, once the
    draft already says plainly it cannot be done and what the person can do instead: proceed.
    escalate_human is for a decision the person must make before the work can go on.

Rules:
  - Redirect or stop the run only when the drift or waste is obvious, EXCEPT a mismatch between an
    action REQUEST and a read-and-answer plan: that is a clear escalate_deep, not a proceed.
  - If a DRAFT ANSWER is shown for an action request: a draft that only recommends, describes, or
    promises the work ("I would recommend", "I can go ahead and", "you could") has NOT done it:
    escalate_deep. A draft that plainly reports what was already done, or fully answers a pure
    question, is fine.
  - If a DRAFT ANSWER is shown and the REQUEST was a QUESTION, check: is the thing asked for actually
    IN the draft? Redirect (hint: answer the question that was asked) when it (a) SUBSTITUTES AN
    OFFER for the answer (a sentence of generality then a proposal to create, track or look into
    something; still a substitution even when the offer is reasonable), or (b) answers a DIFFERENT,
    adjacent question (compare the draft against CURRENT USER REQUEST word for word). A short answer
    is not a failure; an absent one is. Answering and then also offering a next step is fine.
  - If RECENT CONVERSATION shows the user already correcting the assistant ("you ignored my
    question", "that's not what I asked"), treat the next draft with more suspicion: a second miss
    on the same point is a redirect, not a proceed.
  - A REFUSAL IS AN ANSWER: if PRIOR ESCALATIONS shows a proposal was already refused, declined or
    rejected, and the CURRENT PLAN or DRAFT ANSWER is about to put substantially the same proposal to
    the user again (same action on the same objects, however re-worded, re-ordered or re-titled),
    that is a redirect (hint: they already declined this; do what they asked instead of re-asking).
    Only treat it as new if the user themselves asked again, or the request genuinely changed.
  - Keep "reason" to one short sentence, plain and safe to show the user. For escalate_human it IS
    shown to the user as the question they must answer: write it TO them ("Do you want me to...?"),
    never about them ("The user is requesting...").
  - Only set "hint" for a redirect, one short correction.

--- RUN DIGEST ---
{digest}
"""


# ===========================================================================
# Pure digest builder — cheap snapshot, capped at a char budget.
# ===========================================================================

def _oneline(s: Any, limit: int = 160) -> str:
    text = " ".join(str(s or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _cap_section(text: str, cap: int) -> str:
    """Hard-cap one already-rendered, possibly-multi-line SHEDDABLE section to ``cap`` chars, so a
    single section (e.g. a long RECENT CONVERSATION) can never by itself crowd out the others.
    Never raises."""
    try:
        if len(text) <= cap:
            return text
        return text[: max(0, cap - 3)].rstrip() + "..."
    except Exception:  # noqa: BLE001
        return text


# Per-section caps for the SHEDDABLE "history" sections (Fix 8): each is bounded on its own, on top
# of the overall fit-to-budget pass below, so no single history section can dominate the digest.
_RECENT_CONVERSATION_CHAR_CAP = 500
_PRIOR_ESCALATIONS_CHAR_CAP = 300
_OPERATIONS_CHAR_CAP = 700


def build_digest(
    *,
    user_message: str,
    goal_condition: Optional[str] = None,
    step: int,
    max_steps: int,
    plan_action: str = "",
    plan_rationale: str = "",
    plan_goal: str = "",
    recent_conversation: Optional[List[str]] = None,
    prior_escalations: Optional[List[str]] = None,
    operations: Optional[List[str]] = None,
    operations_total: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_seconds: float = 0.0,
    max_elapsed_seconds: float = 0.0,
    gathered_chars: int = 0,
    max_gathered_chars: int = 0,
    consecutive_reads: int = 0,
    draft_answer: Optional[str] = None,
    quality_standards: Optional[str] = None,
    char_budget: int = 1600,
) -> str:
    """Build a compact, one-glance digest of the run for the overseer, capped at ``char_budget``.

    Pure and never-raising. ``user_message`` is the RAW, VERBATIM text the user typed; it is always
    shown so the overseer gets the same word-for-word fidelity the planner gets. ``goal_condition``
    is the RESOLVED, self-contained request (anaphora like "do it" rewritten into the concrete
    instruction); when it differs from ``user_message`` it is shown as an additional ``RESOLVED AS``
    line, never as a silent replacement.

    ``operations`` are already-tagged, one-lined summaries of the operations executed so far THIS
    RUN (e.g. "[read] cli.py -> found argparse subcommands...", produced by the caller so the FULL
    observation bodies never reach the overseer); ``operations_total`` is the true count so far
    (``operations`` may be a trailing window when the run is long). ``recent_conversation`` is a
    handful of PRIOR user turns in this SAME conversation (across turns, not this run); the caller is
    responsible for excluding the current turn's own request so it is never duplicated against
    CURRENT USER REQUEST. ``prior_escalations`` is a caller-supplied history of earlier turns in this
    conversation that already escalated (to deep execution or to a human) and their outcome.

    ``quality_standards``, when present, is the written completion/quality bar the result must clear.
    The ``AGENT'S READ BUDGET`` line reports the MAIN AGENT's own cumulative raw-read volume against
    its read cap, which is unrelated to this digest's own (tiny) size.

    TRUNCATION ORDER (Fix 8): the fields that actually drive a decision -- CURRENT USER REQUEST,
    RESOLVED AS, QUALITY BAR, PASS, CURRENT PLAN, RATIONALE, SPEND, TIME, AGENT'S READ BUDGET, and
    DRAFT ANSWER -- are PROTECTED: they are built first and always included in full. The "history"
    sections (RECENT CONVERSATION, PRIOR ESCALATIONS THIS CONVERSATION, OPERATIONS THIS TURN) are
    SHEDDABLE: each gets its own per-section cap, and if the overall ``char_budget`` is still tight,
    whole sheddable sections are dropped (last-added first) until it fits. Only if the protected
    fields ALONE somehow exceed ``char_budget`` (a pathologically small budget) does a last-resort
    tail-truncation kick in, matching the previous behavior.
    """
    try:
        um = (user_message or "").strip()
        gc = (goal_condition or "").strip()

        # --- PROTECTED head: the user's own request, always first, always in full. -------------
        head: List[str] = [f"CURRENT USER REQUEST: {_oneline(um, 300)}"]
        if gc and gc != um:
            head.append(f"RESOLVED AS: {_oneline(gc, 300)}")
        if quality_standards:
            head.append(f"QUALITY BAR: {_oneline(quality_standards, 200)}")

        # --- SHEDDABLE middle: cross-turn history. Each section individually capped, and this
        # whole block is what gets trimmed/dropped first if the overall budget is tight (Fix 8). ---
        sheddable: List[str] = []

        conv = [c for c in (recent_conversation or []) if c and str(c).strip()]
        if conv:
            n = len(conv)
            lines = [f"RECENT CONVERSATION (last {n} turn{'s' if n != 1 else ''}):"]
            lines.extend(f"  - {_oneline(c, 160)}" for c in conv)
            sheddable.append(_cap_section("\n".join(lines), _RECENT_CONVERSATION_CHAR_CAP))

        esc = [e for e in (prior_escalations or []) if e and str(e).strip()]
        if esc:
            lines = ["PRIOR ESCALATIONS THIS CONVERSATION:"]
            lines.extend(f"  {_oneline(e, 160)}" for e in esc)
            sheddable.append(_cap_section("\n".join(lines), _PRIOR_ESCALATIONS_CHAR_CAP))
        else:
            sheddable.append("PRIOR ESCALATIONS THIS CONVERSATION: none yet")

        ops = [o for o in (operations or []) if o and str(o).strip()]
        if ops:
            total = operations_total if operations_total else len(ops)
            start_num = max(1, total - len(ops) + 1)
            lines = [f"OPERATIONS THIS TURN ({total} so far):"]
            lines.extend(f"  {start_num + i}. {_oneline(o, 160)}" for i, o in enumerate(ops))
            sheddable.append(_cap_section("\n".join(lines), _OPERATIONS_CHAR_CAP))
        else:
            sheddable.append("OPERATIONS THIS TURN: none yet")

        # --- PROTECTED tail: the fields the decision actually turns on. Always included in full. -
        tail: List[str] = [f"PASS: {step} of {max_steps}"]
        plan_bits: List[str] = []
        if plan_action:
            plan_bits.append(f"action={plan_action}")
        if plan_goal:
            plan_bits.append(f"goal={_oneline(str(plan_goal).splitlines()[0] if plan_goal else '', 120)}")
        if plan_bits:
            tail.append("CURRENT PLAN: " + ", ".join(plan_bits))
        if plan_rationale:
            tail.append(f"RATIONALE: {_oneline(plan_rationale, 200)}")
        tail.append(
            f"SPEND: tokens_in={tokens_in} tokens_out={tokens_out}; "
            f"consecutive_reads={consecutive_reads}"
        )
        if max_elapsed_seconds:
            tail.append(f"TIME: {elapsed_seconds:.0f}s of {max_elapsed_seconds:.0f}s budget")
        if max_gathered_chars:
            tail.append(
                f"AGENT'S READ BUDGET: {gathered_chars} of {max_gathered_chars} chars gathered "
                f"so far (the agent's own cumulative reads, NOT this digest's size)"
            )
        if draft_answer:
            tail.append(f"DRAFT ANSWER (first 200 chars): {_oneline(draft_answer, 200)}")

        budget = max(1, int(char_budget))
        full_text = "\n".join(head + sheddable + tail)
        if len(full_text) <= budget:
            return full_text

        # Over budget: drop whole SHEDDABLE sections (last-added first) until it fits. head/tail
        # are NEVER touched here (Fix 8's must-survive guarantee).
        shed = list(sheddable)
        while shed and len("\n".join(head + shed + tail)) > budget:
            shed.pop()
        fitted = "\n".join(head + shed + tail)
        if len(fitted) <= budget:
            return fitted

        # Last resort: even head+tail alone exceed budget (a pathologically small char_budget).
        # Fall back to a hard tail-truncation so the function still returns something bounded.
        return fitted[: max(0, budget - 3)].rstrip() + "..."
    except Exception:  # noqa: BLE001 — a digest hiccup must never break the run
        return _oneline(user_message, min(300, max(1, char_budget)))


# ===========================================================================
# The consultation — one small structured call. Never raises.
# ===========================================================================

def oversee(provider: Any, model: str, digest: str) -> OverseerSignal:
    """Consult the overseer once and return its ``OverseerSignal``.

    Makes ONE structured ``provider.plan`` call (mirroring how the goal verification calls the
    provider). On ANY error, a non-dict response, or an unrecognized signal, returns the safe default
    ``OverseerSignal("proceed")`` so the run is never distorted or broken. Never raises.
    """
    try:
        prompt = OVERSEER_PROMPT.format(digest=digest or "")
        raw = plan_with_step(provider, prompt, model=model, tool_schema=OVERSEE_TOOL,
                            step=STEP_JUDGE)
        if not isinstance(raw, dict):
            return OverseerSignal("proceed", degraded=True)
        signal = str(raw.get("signal") or "").strip().lower()
        if signal not in _VALID_SIGNALS:
            return OverseerSignal("proceed", degraded=True)
        hint = str(raw.get("hint") or "").strip()
        if signal != "redirect":
            hint = ""  # hint is only meaningful for a redirect
        hint = hint[:200]
        reason = str(raw.get("reason") or "").strip()
        return OverseerSignal(signal=signal, hint=hint, reason=reason)
    except Exception:  # noqa: BLE001 — an overseer failure must degrade to proceed, never break
        return OverseerSignal("proceed", degraded=True)
