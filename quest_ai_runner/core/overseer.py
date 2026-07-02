"""Overseer — a minimal-intervention watcher for the Orchestrator run loop.

The idea (Joshua, founder): a HIGH-QUALITY model, reading very few tokens and writing very few,
watches a run the way a human consciousness watches their own body walk. Most of the time it says
nothing; occasionally it sends ONE tiny signal that causes a large downstream course correction.

Modeled on ``core/guard.py``: self-contained, NEVER raises, owns its own prompt/tool constants, and
exposes a PURE digest builder so the caller can decide when to consult it. It knows nothing about
Quest, any org, or the orchestrator's internals beyond a compact digest string. Any internal failure
degrades to the safe default ``OverseerSignal("proceed")`` (do nothing, keep going as if the overseer
were off), so wiring it in can never change a run's outcome except through an explicit signal.

The four signals:
  - ``proceed``    — the run is on track; do nothing (the overwhelming default).
  - ``redirect``   — the plan is drifting off-subject or wasting reads; nudge it with ONE short hint.
  - ``answer_now`` — enough has been gathered; stop reading and answer.
  - ``escalate``   — this genuinely needs deep execution or a human; hand off.

The digest fed to the model is CHEAP: a compact snapshot capped at a char budget, with the last few
observations summarized to one line each (never their full bodies), plus token/time/read counters.
This keeps the overseer's own token cost tiny, which is the whole point.
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
    signal: str = "proceed"          # "proceed" | "redirect" | "answer_now" | "escalate"
    hint: str = ""                   # only for redirect: ONE short course correction
    reason: str = ""                 # one sentence, user-safe


_VALID_SIGNALS = ("proceed", "redirect", "answer_now", "escalate")


# ===========================================================================
# Structured tool + prompt — centralized module constants.
# NO em dashes in any text that may be shown to a user or sent to the model (brand rule).
# ===========================================================================

OVERSEE_TOOL: Dict[str, Any] = {
    "name": "oversee",
    "description": "Emit a single minimal-intervention signal about the run's direction.",
    "input_schema": {
        "type": "object",
        "properties": {
            "signal": {
                "type": "string",
                "enum": list(_VALID_SIGNALS),
                "description": "proceed (default), redirect, answer_now, or escalate.",
            },
            "hint": {
                "type": "string",
                "description": (
                    "ONLY for redirect: one short course correction, under 200 characters. "
                    "Leave empty for every other signal."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One short sentence, safe to show a user, explaining the signal.",
            },
        },
        "required": ["signal"],
    },
}

OVERSEER_PROMPT = """\
You are a minimal-intervention OVERSEER watching an AI assistant work through a request. Think of
yourself as a person's quiet awareness watching their own body walk: almost always you stay silent
and let it proceed, and only rarely do you send one small signal that changes the course.

You are given a compact DIGEST of where the run is right now: the user's request, which planning
pass this is, the current plan step, a few one-line summaries of what has been gathered, and how
much time, tokens, and reading budget have been spent.

Choose EXACTLY ONE signal and return it via the provided tool:
  - "proceed": the run is on a reasonable track. This is your DEFAULT. When unsure, proceed.
  - "redirect": the plan is clearly off-subject, chasing the wrong thing, or wasting reads on
    material that will not answer the request. Give a "hint" that is ONE short course correction
    (what to focus on instead), under 200 characters. Do not write a plan, just the nudge.
  - "answer_now": enough has already been gathered to answer the user well; more reading is waste.
  - "escalate": the request is really asking to DO something (make a code or file change, run or
    commit work, fix-and-verify, take a real action), or it needs a human decision. Reading and
    writing an answer cannot satisfy such a request. If the REQUEST uses an action verb (add, fix,
    implement, change, create, run, commit, send, delete, refactor) and the plan is only reading or
    drafting an answer ABOUT the work rather than executing it, choose escalate.

Rules:
  - Bias hard toward "proceed". Only redirect or stop the run when the drift or waste is obvious.
    The one thing NOT to be timid about is a mismatch between an action REQUEST and a read-and-answer
    plan: that mismatch is a clear escalate, not a proceed.
  - If a DRAFT ANSWER is shown, judge whether it actually REPORTS completed work. For an action
    request, a draft that only recommends, describes, or promises the work ("I would recommend",
    "I can go ahead and", "the next step would be", "you could") has NOT done it: escalate. A draft
    that plainly reports what was already done, or that fully answers a pure question, is fine.
  - Keep "reason" to one short sentence, plain and safe to show the user.
  - Only set "hint" for a redirect, and keep it to a single short correction.
  - Do NOT use em dashes anywhere in your output. Use a comma, a colon, parentheses, or two
    sentences instead.

--- RUN DIGEST ---
{digest}
"""


# ===========================================================================
# Pure digest builder — cheap snapshot, capped at a char budget.
# ===========================================================================

def _oneline(s: Any, limit: int = 160) -> str:
    text = " ".join(str(s or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def build_digest(
    *,
    question: str,
    step: int,
    max_steps: int,
    plan_action: str = "",
    plan_rationale: str = "",
    plan_goal: str = "",
    observation_summaries: Optional[List[str]] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_seconds: float = 0.0,
    max_elapsed_seconds: float = 0.0,
    gathered_chars: int = 0,
    max_gathered_chars: int = 0,
    consecutive_reads: int = 0,
    draft_answer: Optional[str] = None,
    quality_standards: Optional[str] = None,
    char_budget: int = 1200,
) -> str:
    """Build a compact, one-glance digest of the run for the overseer, capped at ``char_budget``.

    Pure and never-raising. ``observation_summaries`` are already-one-lined summaries of the last
    few gathered observations (the caller produces them, e.g. via the orchestrator's
    ``_summarize_observation``, so the FULL observation bodies never reach the overseer). Everything
    else is a small counter. ``question`` should be the RESOLVED, self-contained request (the
    orchestrator's ``goal_condition``), not the raw surface text, so the overseer judges the run
    against what it is actually trying to do. ``quality_standards``, when present, is the written
    completion/quality bar the result must clear; it is added as a single ``QUALITY BAR`` line so the
    overseer can tell a done answer from an unmet one. The ``AGENT'S READ BUDGET`` line reports the
    MAIN AGENT's own cumulative raw-read volume against its read cap, which is unrelated to this
    digest's own (tiny) size. The final string is hard-truncated to ``char_budget`` so the overseer's
    read cost stays tiny by construction.
    """
    try:
        lines: List[str] = []
        lines.append(f"REQUEST: {_oneline(question, 300)}")
        if quality_standards:
            lines.append(f"QUALITY BAR: {_oneline(quality_standards, 200)}")
        lines.append(f"PASS: {step} of {max_steps}")

        plan_bits: List[str] = []
        if plan_action:
            plan_bits.append(f"action={plan_action}")
        if plan_goal:
            plan_bits.append(f"goal={_oneline(str(plan_goal).splitlines()[0] if plan_goal else '', 120)}")
        if plan_bits:
            lines.append("CURRENT PLAN: " + ", ".join(plan_bits))
        if plan_rationale:
            lines.append(f"RATIONALE: {_oneline(plan_rationale, 200)}")

        summaries = observation_summaries or []
        recent = summaries[-8:]
        if recent:
            lines.append(f"GATHERED (last {len(recent)} of {len(summaries)}):")
            for s in recent:
                lines.append(f"  - {_oneline(s, 160)}")
        else:
            lines.append("GATHERED: nothing yet")

        lines.append(
            f"SPEND: tokens_in={tokens_in} tokens_out={tokens_out}; "
            f"consecutive_reads={consecutive_reads}"
        )
        if max_elapsed_seconds:
            lines.append(
                f"TIME: {elapsed_seconds:.0f}s of {max_elapsed_seconds:.0f}s budget"
            )
        if max_gathered_chars:
            lines.append(
                f"AGENT'S READ BUDGET: {gathered_chars} of {max_gathered_chars} chars gathered "
                f"so far (the agent's own cumulative reads, NOT this digest's size)"
            )
        if draft_answer:
            lines.append(f"DRAFT ANSWER (first 200 chars): {_oneline(draft_answer, 200)}")

        digest = "\n".join(lines)
        budget = max(1, int(char_budget))
        if len(digest) > budget:
            digest = digest[: budget - 3].rstrip() + "..."
        return digest
    except Exception:  # noqa: BLE001 — a digest hiccup must never break the run
        return _oneline(question, min(300, max(1, char_budget)))


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
