"""Broken-promise guard — post-turn honesty check for the Orchestrator.

The risk this manages (Joshua, 2026-06-15): the assistant SAYS it did something, or that it
will, when the action did not actually execute or finish. Chosen behavior: AUTO-REMEDIATE THEN
VERIFY. At turn finalization the guard:

  1. Captures durable EXECUTION FACTS for the turn (did a mutating action run; did it SUCCEED or
     FAIL), built from the deep run results (``DeepResult.met``) and any ``EVENT_EXEC`` lifecycle
     ticks the deep runner emitted (phase ``done`` = a success, ``error`` / exhausted ``retry`` =
     a failure).
  2. STRUCTURALLY GATES (cheap): only engages when the reply TEXT actually asserts a completed or
     imminent action (claim signals like "I've added/created/updated/...", "I will <do>"). No
     claim signal -> pass through unchanged, ZERO model cost.
  3. When a claim is present, runs a focused verification (one small ``ModelProvider`` call) that
     decides whether the turn's execution record SUPPORTS the claim.
  4. On a mismatch: if the action clearly did NOT run at all (no success AND no failure recorded)
     and re-running is SAFE, the orchestrator attempts ONE remediation pass then re-verifies; if
     still unmet, or remediation is unsafe/uncertain, the reply is rewritten to be HONEST and the
     result is flagged ``partial`` so a background task maps to needs_you / failed, not done.

CRITICAL SAFETY — NEVER cause a double mutation. Remediation (re-running the action) is permitted
ONLY when we are confident the action did NOT already execute successfully this turn. When the
execution record shows ANY successful OR failed mutation, or when it is empty but uncertain, the
guard does NOT re-run — it falls back to honest correction. Host actions are not guaranteed
idempotent, so the bias is always: prefer honest-correction over risky re-execution.

The guard is APP-AGNOSTIC (no Quest/org specifics), and NEVER raises: any internal failure
degrades to leaving the turn exactly as it was (the reply and status unchanged).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ===========================================================================
# Execution facts — the durable per-turn record of what actually executed.
# ===========================================================================

@dataclass
class ExecutionFact:
    """One mutating-action outcome observed this turn.

    ``goal`` is the deep goal/brief the action was run under (human-readable). ``succeeded`` /
    ``failed`` are derived from ``DeepResult.met`` and any ``EVENT_EXEC`` phase ticks. A fact with
    neither succeeded nor failed means the action was REQUESTED but produced no recorded outcome
    (e.g. no runner wired) — the one case where safe remediation is possible.
    """
    goal: str = ""
    succeeded: bool = False
    failed: bool = False
    error: Optional[str] = None
    # Raw EVENT_EXEC phase ticks seen for this action (for tracing; not required for the decision).
    phases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"goal": self.goal, "succeeded": self.succeeded, "failed": self.failed}
        if self.error:
            d["error"] = self.error
        if self.phases:
            d["phases"] = self.phases
        return d


@dataclass
class ExecutionRecord:
    """The whole turn's execution facts, attached to ``OrchestratorResult.execution_record``."""
    facts: List[ExecutionFact] = field(default_factory=list)

    @property
    def any_mutation_attempted(self) -> bool:
        return bool(self.facts)

    @property
    def any_success(self) -> bool:
        return any(f.succeeded for f in self.facts)

    @property
    def any_failure(self) -> bool:
        return any(f.failed for f in self.facts)

    def summary(self) -> str:
        """A compact, human/LLM-readable rendering of what actually executed this turn."""
        if not self.facts:
            return "NO action/operation executed this turn (no mutating work ran)."
        lines: List[str] = []
        for f in self.facts:
            if f.succeeded:
                state = "SUCCEEDED"
            elif f.failed:
                state = "FAILED" + (f" ({f.error})" if f.error else "")
            else:
                state = "NO RECORDED OUTCOME (requested but neither success nor failure recorded)"
            lines.append(f"- action {f.goal!r}: {state}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {"facts": [f.to_dict() for f in self.facts]}


# Phases (consumer-defined on EVENT_EXEC ``data``) that signal a terminal outcome for an action.
_SUCCESS_PHASES = {"done", "succeeded", "success", "complete", "completed"}
_FAILURE_PHASES = {"error", "failed", "failure"}
# A retry tick on its own is in-flight, not terminal; only an EXHAUSTED retry would be a failure,
# which the deep runner reports via DeepResult.met=False (the authoritative signal we already use).


def classify_exec_phase(phase: Optional[str]) -> Optional[str]:
    """Map an EVENT_EXEC phase string to "success" | "failure" | None (non-terminal). Never raises."""
    if not isinstance(phase, str):
        return None
    p = phase.strip().lower()
    if p in _SUCCESS_PHASES:
        return "success"
    if p in _FAILURE_PHASES:
        return "failure"
    return None


# ===========================================================================
# Structural claim detection — the cheap gate. No model cost.
# ===========================================================================

# Past-tense / present completed-action claims ("I've added", "I added", "I have created",
# "I've gone ahead and updated", "Done — created", "I marked", "I set up", "I scheduled").
# Kept app-agnostic: a broad set of mutate verbs, not Quest-specific nouns.
_DONE_VERBS = (
    r"add(?:ed)?|creat(?:ed|e)|updat(?:ed|e)|sav(?:ed|e)|set(?: up)?|schedul(?:ed|e)|"
    r"mark(?:ed)?|sen[dt]|delet(?:ed|e)|remov(?:ed|e)|chang(?:ed|e)|edit(?:ed)?|"
    r"appli(?:ed)?|appl(?:y)|post(?:ed)?|submit(?:ted)?|complet(?:ed|e)|"
    r"renam(?:ed|e)|moved?|configur(?:ed|e)|enabl(?:ed|e)|disabl(?:ed|e)|fix(?:ed)?|implement(?:ed)?"
)

# "I've / I have / I just / I went ahead and <verb>" or "I <verb-ed>" — a completed-action claim.
_CLAIM_DONE_RE = re.compile(
    r"\bI(?:'ve| have| just| already)?\s+(?:gone ahead and\s+|now\s+)?(?:" + _DONE_VERBS + r")\b",
    re.IGNORECASE,
)
# "Done", "All set", "That's done", "Successfully <verb>ed" — completion announcements.
_CLAIM_ANNOUNCE_RE = re.compile(
    r"\b(?:done|all set|that's done|successfully|i['’]ve gone ahead|here you go)\b",
    re.IGNORECASE,
)
# Progressive ("-ing") stems of the mutate verbs, so "I'm adding/creating/updating" is caught but
# benign progressives ("I'm thinking", "I am wondering") are not.
_PROGRESSIVE_VERBS = (
    r"add|creat|updat|sav|sett|schedul|mark|send|delet|remov|chang|edit|"
    r"apply|post|submitt|complet|renam|mov|configur|enabl|disabl|fix|implement"
)
# "I will / I'll / I'm going to <mutate-verb>" or "I'm <mutate-verb>ing" — an imminent-commitment
# claim. Requires an actual mutate verb so benign futures ("I'll note that", "I am thinking") do NOT
# fire the verification call.
_CLAIM_FUTURE_RE = re.compile(
    r"\bI(?:'ll| will| am going to| am about to)\s+(?:go ahead and\s+|now\s+)?(?:" + _DONE_VERBS + r")\b"
    r"|\bI(?:'m| am)\s+(?:now\s+)?(?:" + _PROGRESSIVE_VERBS + r")ing\b",
    re.IGNORECASE,
)


def text_claims_action(text: Optional[str]) -> bool:
    """True iff the reply TEXT asserts a completed or imminent action (a claim signal).

    Structural and cheap — this is the gate that keeps the guard off plain informational replies
    (zero model cost on good turns). Never raises.
    """
    if not text or not text.strip():
        return False
    try:
        t = text
        if _CLAIM_DONE_RE.search(t):
            return True
        if _CLAIM_ANNOUNCE_RE.search(t):
            return True
        if _CLAIM_FUTURE_RE.search(t):
            return True
    except Exception:  # noqa: BLE001 — a regex hiccup must never break the turn
        return False
    return False


# ===========================================================================
# Verification + honest rewrite prompts — centralized module constants.
# NO em dashes in any text that may be shown to a user (brand rule).
# ===========================================================================

VERIFY_CLAIM_PROMPT = """\
You are an honesty checker for an AI assistant's reply.

The assistant just produced the REPLY below. Separately, here is the RECORD of what the assistant
actually executed this turn (the only mutating actions that truly ran, and whether each succeeded
or failed).

Decide ONE thing: does the EXECUTION RECORD support the action the reply claims to have done or to
be doing? Answer strictly:
  - "supported"   : every completed action the reply claims is backed by a SUCCEEDED entry in the
                    record (or the reply only promises a future step that is genuinely under way).
  - "unsupported" : the reply claims or implies it COMPLETED an action that the record does not show
                    succeeding (it failed, or never ran), i.e. the reply overstates what happened.

Be strict: if the reply says it added / created / updated / saved / sent / scheduled / marked /
fixed something but the record shows no SUCCEEDED action matching that, answer "unsupported".

Reply ONLY with a JSON object via the provided tool.

--- REPLY ---
{reply}

--- EXECUTION RECORD (what actually ran) ---
{record}
"""

VERIFY_TOOL: Dict[str, Any] = {
    "name": "verdict",
    "description": "Record whether the reply's action claim is supported by the execution record.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["supported", "unsupported"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict"],
    },
}

HONEST_REWRITE_PROMPT = """\
You are correcting an AI assistant's reply so it is HONEST about what actually happened.

The DRAFT reply below overstates what the assistant did: it claims or implies an action completed
that did NOT actually succeed this turn. Rewrite the reply so it states plainly what actually
happened and what did NOT. Follow these rules:
  - NEVER claim success for an action that did not succeed.
  - If the action failed, say so plainly and briefly, and that you could not complete it.
  - If nothing ran, say you did not make the change (do not pretend you tried something you did not).
  - Keep the user's original intent visible: acknowledge what they asked for.
  - Be brief, direct, and natural. Do NOT apologize repeatedly.
  - Do NOT use em dashes. Use a comma, a colon, parentheses, or two sentences instead.

--- DRAFT REPLY (overstates what happened) ---
{reply}

--- WHAT ACTUALLY HAPPENED THIS TURN ---
{record}

Write only the corrected reply text.
"""

# A deterministic fallback used if the rewrite model call itself fails — still honest, no em dashes.
HONEST_FALLBACK_TEXT = (
    "I was not able to complete that action, so I have not made the change. "
    "Let me know if you would like me to try again."
)


def verify_supported(provider: Any, model: str, reply: str, record: ExecutionRecord) -> bool:
    """Return True iff a focused model check says the reply's claim IS supported by the record.

    Conservative default: on ANY error or an unparseable verdict, returns True (treat as supported)
    so the guard never degrades a correct turn or crashes it. Never raises.
    """
    try:
        prompt = VERIFY_CLAIM_PROMPT.format(reply=reply or "", record=record.summary())
        raw = provider.plan(prompt, model=model, tool_schema=VERIFY_TOOL)
        if not isinstance(raw, dict):
            return True
        verdict = str(raw.get("verdict") or "").strip().lower()
        return verdict != "unsupported"
    except Exception:  # noqa: BLE001 — verification failure must not break or distort the turn
        return True


def honest_rewrite(provider: Any, model: str, reply: str, record: ExecutionRecord) -> str:
    """Rewrite ``reply`` to be honest about the record. Never raises; falls back to a safe note."""
    try:
        prompt = HONEST_REWRITE_PROMPT.format(reply=reply or "", record=record.summary())
        out = provider.answer([{"role": "user", "content": prompt}], model=model)
        if isinstance(out, str) and out.strip():
            return out.strip()
    except Exception:  # noqa: BLE001
        pass
    return HONEST_FALLBACK_TEXT
