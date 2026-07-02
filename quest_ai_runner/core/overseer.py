"""Overseer — a minimal-intervention watcher for the Orchestrator run loop.

The idea (Joshua, founder): a HIGH-QUALITY model, reading very few tokens and writing very few,
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


# ===========================================================================
# The signal — the overseer's whole output.
# ===========================================================================

@dataclass
class OverseerSignal:
    """One overseer decision. ``proceed`` is the do-nothing default.

    ``hint`` is only meaningful for ``redirect``: a single short course correction (kept under
    ~200 chars). ``reason`` is a one-sentence, user-safe explanation of the signal (may be surfaced
    as the overseer event's text), and is empty for a plain proceed.
    """
    signal: str = "proceed"    # "proceed" | "redirect" | "answer_now" | "escalate_deep" | "escalate_human"
    hint: str = ""              # only for redirect: ONE short course correction
    reason: str = ""             # one sentence, user-safe


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
You are a minimal-intervention OVERSEER watching an AI assistant work through a request. Think of
yourself as a person's quiet awareness watching their own body walk: almost always you stay silent
and let it proceed, and only rarely do you send one small signal that changes the course.

You are given a compact DIGEST of where the run is right now. The DIGEST fields, and why each
matters to your judgment:
  - CURRENT USER REQUEST (+ RESOLVED AS, when present): the user's literal words, plus what they
    were resolved to; judge the run against this actual request, not a sibling topic.
  - RECENT CONVERSATION: prior turns in this same thread; use it to catch drift from what the user
    has actually been asking across turns.
  - PRIOR ESCALATIONS THIS CONVERSATION: whether an earlier turn already needed deep work or a
    human; repeated escalation with no progress is itself a signal.
  - OPERATIONS THIS TURN: exactly what has been read or searched so far this run; use it to catch
    redundant or off-topic reads.
  - PASS: which planning pass this is out of the cap; more passes with no progress is a signal.
  - CURRENT PLAN: what the run is about to do next; check it still serves the request.
  - RATIONALE: the planner's own stated reason for that plan; check it actually supports the plan.
  - SPEND: tokens burned so far; a rough cost signal, not a hard stop on its own.
  - TIME: wall-clock spent against budget; nearing the cap without a path to an answer is a signal.
  - AGENT'S READ BUDGET: the agent's own cumulative read volume against its cap (not this digest's
    size); nearing it with nothing useful found is a signal to answer_now.
  - QUALITY BAR: the completion standard the result must clear; a draft that ignores it is not done.
  - DRAFT ANSWER: the proposed reply, only present at the final checkpoint; judge whether it truly
    satisfies the request and the quality bar.

Choose EXACTLY ONE signal and return it via the provided tool:
  - "proceed": the run is on a reasonable track. This is your DEFAULT. When unsure, proceed.
  - "redirect": the plan is clearly off-subject, chasing the wrong thing, or wasting reads on
    material that will not answer the request. Give a "hint" that is ONE short course correction
    (what to focus on instead), under 200 characters. Do not write a plan, just the nudge.
  - "answer_now": enough has already been gathered to answer the user well; more reading is waste.
  - "escalate_deep": the request is really asking to DO something (make a code or file change, run
    or commit work, fix-and-verify, take a real action). Reading and writing an answer cannot
    satisfy such a request, but this is ROUTINE, AI-doable work, not a human decision. If the REQUEST
    uses an action verb (add, fix, implement, change, create, run, commit, send, delete, refactor)
    and the plan is only reading or drafting an answer ABOUT the work rather than executing it,
    choose escalate_deep.
  - "escalate_human": this is a genuine HUMAN-ONLY fork, not routine automatable work. Reserve this
    for identity questions, an irreversible or authorization-requiring action (e.g. an outward
    payment, a real-world commitment, deleting something unrecoverable), or a genuine ambiguity
    only the user/owner can resolve (a taste call, a direction call). This mirrors the org principle
    that AI acts first and only genuine forks go to a human: escalate_human must NOT fire just
    because a task is hard, unclear in a resolvable way, or merely needs more digging. When in
    doubt between escalate_deep and escalate_human, prefer escalate_deep; reserve escalate_human for
    cases an AI plainly should not decide or execute on its own.

Rules:
  - Only redirect or stop the run when the drift or waste is obvious. The one thing NOT to be timid
    about is a mismatch between an action REQUEST and a read-and-answer plan: that mismatch is a
    clear escalate_deep, not a proceed.
  - If a DRAFT ANSWER is shown, judge whether it actually REPORTS completed work. For an action
    request, a draft that only recommends, describes, or promises the work ("I would recommend",
    "I can go ahead and", "the next step would be", "you could") has NOT done it: escalate_deep. A
    draft that plainly reports what was already done, or that fully answers a pure question, is fine.
  - Keep "reason" to one short sentence, plain and safe to show the user.
  - Only set "hint" for a redirect, and keep it to a single short correction.

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

    Makes ONE structured ``provider.plan`` call (mirroring how ``guard.verify_supported`` calls the
    provider). On ANY error, a non-dict response, or an unrecognized signal, returns the safe default
    ``OverseerSignal("proceed")`` so the run is never distorted or broken. Never raises.
    """
    try:
        prompt = OVERSEER_PROMPT.format(digest=digest or "")
        raw = provider.plan(prompt, model=model, tool_schema=OVERSEE_TOOL)
        if not isinstance(raw, dict):
            return OverseerSignal("proceed")
        signal = str(raw.get("signal") or "").strip().lower()
        if signal not in _VALID_SIGNALS:
            return OverseerSignal("proceed")
        hint = str(raw.get("hint") or "").strip()
        if signal != "redirect":
            hint = ""  # hint is only meaningful for a redirect
        hint = hint[:200]
        reason = str(raw.get("reason") or "").strip()
        return OverseerSignal(signal=signal, hint=hint, reason=reason)
    except Exception:  # noqa: BLE001 — an overseer failure must degrade to proceed, never break
        return OverseerSignal("proceed")
