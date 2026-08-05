"""The USER-FACING account of how a turn reached its answer ("Explain how I got this").

This is NOT the internal reasoning narration. That channel relayed the run's OWN wording (plan
rationales, "Understood as: ...", partial narration beats) and was deliberately removed from what
the reader sees. What this module produces is different in kind: wording composed FOR THE READER,
about the run, plus a genuine RECORD of what the run actually touched.

The split matters, so it is encoded in the payload shape rather than left to a convention:

  * ``used`` and ``signals`` are FACTS. They are assembled here from the turn's real trace (the
    observations the gather steps produced, the execution record, the assembled cards and sources,
    the goal verdict, the exit reason). No model writes them. They can be checked.
  * ``understood``, ``approach``, ``assumptions``, ``confidence``, ``limitations`` and
    ``what_would_change`` are a model-written summary, constrained to that same trace. They are a
    reconstruction, not a transcript of anything, which is why the consumer shows a disclaimer.

Two invariants this module exists to hold:

1. **Eligibility is model-free.** ``is_eligible`` is a boolean over the trace: reads happened, or
   actions executed, or a deep run happened, or context reached the answer, or the planner took
   more than one step, or the web was searched. It costs nothing, needs no threshold tuning, and
   cannot itself be wrong in an expensive way. A plain "Hi" takes the small-talk short circuit,
   gathers nothing, answers at step 0, and so is ineligible for free: no event, no toggle.
2. **The explanation may never assert a step the record does not show.** The generation prompt is
   given the execution record and told to describe only what is in it. This is the same failure the
   goal verifier already guards with ``claims_unexecuted``: a confident account of tool calls that
   never happened is worse than no account at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# The payload version a consumer can branch on if the shape ever grows.
EXPLANATION_VERSION = 1

# How many items of any one kind survive into the payload. The panel is a summary, not a log, and
# the payload is persisted on the message, so it stays small on purpose.
MAX_ITEMS_PER_KIND = 12

# The marker both web-search retrieval adapters stamp on an observation's ``rel_path``
# (``WebSearchAdapter`` and ``ProviderWebSearchAdapter`` both use ``web_search:<query>``).
WEB_OBSERVATION_PREFIX = "web_search:"


EXPLAIN_TOOL: Dict[str, Any] = {
    "name": "answer_explanation",
    "description": (
        "Write the plain-language account, FOR THE PERSON WHO ASKED, of how this answer was "
        "reached. Not a log, not internal notes: a short, readable explanation they can judge the "
        "answer by."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "understood": {
                "type": "string",
                "description": "One or two sentences, addressed to the person as 'you': what their "
                               "question was taken to be. Plain language, no ids, no file names, "
                               "no internal vocabulary.",
            },
            "approach": {
                "type": "string",
                "description": "One or two sentences naming HOW the answer was worked out (for "
                               "example: compared two options, calculated from figures found, "
                               "ruled out alternatives, summarized what the documents said). "
                               "Describe the shape of the reasoning, not the tools.",
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 assumptions the answer rests on that the person did not "
                               "state. Empty when the answer rests on nothing unstated.",
            },
            "confidence": {
                "type": "string",
                "description": "One sentence on how confident this answer is and why, grounded in "
                               "what was actually available.",
            },
            "limitations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 concrete limits: what was not checked, what was missing, "
                               "where the answer is thin. Empty when there are none worth naming.",
            },
            "what_would_change": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 specific things that would change this answer if they "
                               "turned out differently.",
            },
        },
        "required": ["understood", "approach", "confidence"],
    },
}


EXPLAIN_PROMPT = """\
You are writing a short "how I got this" note that will be shown to the person who asked, directly
under the answer they just received. They can expand it to check the answer's reasoning.

Write FOR THEM. Address them as "you". Do not write notes to yourself, do not describe your own
process in machine terms, and never mention tools, ids, adapters, cards, prompts, steps, models,
goal conditions, or anything else from the machinery. If a document or file genuinely informs the
answer, describe it the way a person would name it.

--- WHAT THEY ASKED ---
{user_message}

--- WHAT THE REQUEST WAS RESOLVED TO ---
{goal_condition}

--- THE ANSWER THEY WERE GIVEN ---
{answer}

--- THE RECORD OF WHAT ACTUALLY HAPPENED THIS TURN ---
{record}

HARD RULES:
- Describe ONLY what the record above shows. Do NOT say something was looked up, read, checked,
  run, saved or sent unless the record shows it. If the record shows no retrieval and no actions,
  then the answer came from what was already known and from their own message, and you must say so
  plainly rather than inventing steps.
- Do NOT invent WHERE a fact came from. Never name a source, log, system, report or record that the
  record above does not name. An action's outcome is known because the action was run, not because
  some log was consulted.
- Do not restate the answer. They have already read it.
- Do not quote their own words back at them.
- Be brief. Each sentence should earn its place. Prefer three short sentences to one long one.
- Never use an em dash. Split the sentence, or use a comma, colon, or parentheses.
{language}"""


@dataclass
class TurnTrace:
    """The real, model-free record of one turn, assembled from what the loop already produced.

    Every field here is something the run OBSERVED, never something it said. This is both the input
    the explanation is constrained to and the source of the payload's ``used`` / ``signals``
    sections, which the consumer renders as a record rather than as prose.
    """

    kind: str = "answer"
    user_message: str = ""
    goal_condition: str = ""
    answer: str = ""
    steps: int = 0
    exit_reason: str = ""
    goal_verdict: Optional[Dict[str, Any]] = None
    # Observation dicts as produced by ``Observation.to_dict`` (kind/rel_path/locator/pattern/hits).
    gathered: List[Dict[str, Any]] = field(default_factory=list)
    # ``ExecutionRecord.to_dict()["facts"]`` entries: {goal, succeeded, failed, error?}.
    actions: List[Dict[str, Any]] = field(default_factory=list)
    # Already-projected card metadata and sources, i.e. the same safe shapes EVENT_CONTEXT streams
    # (labels, counts and path-like items only, never the material itself).
    cards: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)

    def read_observations(self) -> List[Dict[str, Any]]:
        """The gather results that represent something actually read or searched (errors dropped)."""
        out: List[Dict[str, Any]] = []
        for obs in self.gathered or []:
            if not isinstance(obs, dict):
                continue
            if obs.get("kind") == "error":
                continue
            out.append(obs)
        return out

    def used_web(self) -> bool:
        for obs in self.gathered or []:
            if not isinstance(obs, dict):
                continue
            rel = str(obs.get("rel_path") or "")
            if rel.startswith(WEB_OBSERVATION_PREFIX):
                return True
            if str(obs.get("locator") or "").startswith("web extract"):
                return True
        return False


def trace_from_result(result: Any, *, user_message: str, goal_condition: str,
                      cards: Optional[List[Dict[str, Any]]] = None,
                      sources: Optional[List[Dict[str, Any]]] = None) -> TurnTrace:
    """Assemble a TurnTrace from a terminal ``OrchestratorResult``. Pure assembly, no model work.

    ``cards`` and ``sources`` are the ALREADY-PROJECTED shapes the turn streamed on EVENT_CONTEXT.
    They are passed in rather than re-derived so that what the panel shows and what the internal
    context event showed can never disagree, and so nothing unprojected can leak in by a new route.
    """
    actions: List[Dict[str, Any]] = []
    try:
        record = getattr(result, "execution_record", None)
        if record is not None:
            actions = list((record.to_dict() or {}).get("facts") or [])
    except Exception:  # noqa: BLE001 -- trace assembly must never break a turn
        actions = []
    answer = ""
    try:
        if getattr(result, "kind", "") == "deep":
            answer = "\n\n".join(
                s for s in (getattr(d, "output", "") or "" for d in (result.deep_results or [])) if s
            )
        else:
            answer = getattr(result, "text", "") or ""
    except Exception:  # noqa: BLE001
        answer = getattr(result, "text", "") or ""
    return TurnTrace(
        kind=getattr(result, "kind", "answer") or "answer",
        user_message=user_message or "",
        goal_condition=goal_condition or "",
        answer=answer,
        steps=int(getattr(result, "steps", 0) or 0),
        exit_reason=getattr(result, "exit_reason", "") or "",
        goal_verdict=getattr(result, "goal_verdict", None),
        gathered=list(getattr(result, "gathered", None) or []),
        actions=actions,
        cards=list(cards or []),
        sources=list(sources or []),
    )


def is_eligible(trace: TurnTrace) -> bool:
    """Is this turn worth explaining? A boolean over the real trace. No model call, no threshold.

    True when the run did something a person could reasonably want accounted for: it read or
    searched, it executed an action, it ran deep, it answered on assembled context, it took more
    than one planning step, or it went to the web. False otherwise, which is exactly the plain
    small-talk case (no retrieval, no actions, answered at the first step).

    Only answer and deep turns are ever eligible. A confirm, a clarification and a cancelled run
    are not answers, so there is nothing to explain.
    """
    if trace.kind not in ("answer", "deep"):
        return False
    if not (trace.answer or "").strip():
        return False
    if trace.kind == "deep":
        return True
    if trace.read_observations():
        return True
    if trace.actions:
        return True
    if trace.cards or trace.sources:
        return True
    if trace.steps > 1:
        return True
    if trace.used_web():
        return True
    return False


def render_record_for_prompt(trace: TurnTrace) -> str:
    """The turn's facts as a compact block the generation call is CONSTRAINED to.

    Deliberately blunt about emptiness: when nothing was retrieved and nothing executed, this says
    so in words, because that is the case where a model is most tempted to invent a process.
    """
    lines: List[str] = []
    reads = trace.read_observations()
    if reads:
        lines.append(f"Looked at {len(reads)} thing(s):")
        for obs in reads[:MAX_ITEMS_PER_KIND]:
            kind = str(obs.get("kind") or "read")
            where = str(obs.get("rel_path") or obs.get("pattern") or obs.get("locator") or "").strip()
            locator = str(obs.get("locator") or "").strip()
            detail = f" ({locator})" if locator and locator != where else ""
            lines.append(f"  - {kind}: {where[:160]}{detail[:80]}")
    else:
        lines.append("Nothing was read, searched or looked up this turn.")
    if trace.cards or trace.sources:
        labels = [str(c.get("title") or "").strip() for c in trace.cards if isinstance(c, dict)]
        labels += [str(s.get("label") or "").strip() for s in trace.sources if isinstance(s, dict)]
        labels = [x for x in labels if x][:MAX_ITEMS_PER_KIND]
        if labels:
            lines.append("Background context available while answering: " + "; ".join(labels))
    else:
        lines.append("No background context was assembled for this turn.")
    if trace.actions:
        lines.append("Actions attempted:")
        for act in trace.actions[:MAX_ITEMS_PER_KIND]:
            if act.get("succeeded"):
                state = "SUCCEEDED"
            elif act.get("failed"):
                state = "FAILED" + (f" ({act.get('error')})" if act.get("error") else "")
            else:
                state = "NO RECORDED OUTCOME"
            lines.append(f"  - {str(act.get('goal') or '')[:160]}: {state}")
    else:
        lines.append("No action was executed this turn (nothing was changed, saved or sent).")
    if trace.used_web():
        lines.append("The web was searched this turn.")
    verdict = trace.goal_verdict or {}
    if verdict:
        met = "yes" if verdict.get("met") else "no"
        reason = str(verdict.get("reason") or "").strip()
        lines.append(f"Answer checked against what was asked for: met={met}."
                     + (f" Checker's note: {reason}" if reason else ""))
        if verdict.get("claims_unexecuted"):
            lines.append("WARNING: the answer claimed a change the record does not back. Say so.")
    lines.append(f"Planning steps taken: {trace.steps}.")
    return "\n".join(lines)


def render_used(trace: TurnTrace) -> Dict[str, Any]:
    """The ``used`` section: a RECORD of what informed the answer, never model prose."""
    cards: List[Dict[str, Any]] = []
    for c in (trace.cards or [])[:MAX_ITEMS_PER_KIND]:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or "").strip()
        if title:
            cards.append({"title": title, "adapter": str(c.get("adapter") or "")})
    sources: List[Dict[str, Any]] = []
    for s in (trace.sources or [])[:MAX_ITEMS_PER_KIND]:
        if isinstance(s, str):
            sources.append({"label": s, "adapter": "", "item_count": 0})
            continue
        if not isinstance(s, dict):
            continue
        label = str(s.get("label") or s.get("adapter") or "").strip()
        if label:
            sources.append({"label": label, "adapter": str(s.get("adapter") or ""),
                            "item_count": int(s.get("item_count") or 0)})
    reads: List[Dict[str, Any]] = []
    for obs in trace.read_observations()[:MAX_ITEMS_PER_KIND]:
        path = str(obs.get("rel_path") or obs.get("pattern") or "").strip()
        if not path:
            continue
        reads.append({"kind": str(obs.get("kind") or "read"),
                      "path": path[:200],
                      "locator": str(obs.get("locator") or "")[:120]})
    actions: List[Dict[str, Any]] = []
    for act in (trace.actions or [])[:MAX_ITEMS_PER_KIND]:
        if not isinstance(act, dict):
            continue
        state = "succeeded" if act.get("succeeded") else ("failed" if act.get("failed") else "unknown")
        actions.append({"goal": str(act.get("goal") or "")[:200], "state": state})
    return {"cards": cards, "sources": sources, "reads": reads,
            "actions": actions, "web": trace.used_web()}


def render_signals(trace: TurnTrace) -> Dict[str, Any]:
    """The ``signals`` section: the run's own outcome facts, for the confidence block."""
    verdict = trace.goal_verdict or {}
    met = verdict.get("met")
    return {
        "exit_reason": trace.exit_reason or "",
        "goal_met": bool(met) if met is not None else None,
        "verdict_reason": str(verdict.get("reason") or "").strip(),
        "claims_unexecuted": bool(verdict.get("claims_unexecuted")),
        "steps": trace.steps,
        "deep": trace.kind == "deep",
    }


def clean_list(raw: Any, limit: int = 4) -> List[str]:
    """Coerce a model-returned array field into a short list of clean strings. Never raises."""
    out: List[str] = []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return out
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text[:400])
        if len(out) >= limit:
            break
    return out


def build_payload(trace: TurnTrace, written: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble the wire payload from the trace plus (optionally) the model-written sections.

    ``written`` is None when the generation call failed or returned nothing usable. That is NOT a
    reason to drop the panel: the ``used`` and ``signals`` sections are the verifiable half and
    stand on their own. The consumer simply has fewer prose sections to render.
    """
    payload: Dict[str, Any] = {
        "version": EXPLANATION_VERSION,
        "used": render_used(trace),
        "signals": render_signals(trace),
    }
    if isinstance(written, dict):
        understood = str(written.get("understood") or "").strip()
        approach = str(written.get("approach") or "").strip()
        confidence = str(written.get("confidence") or "").strip()
        if understood:
            payload["understood"] = understood[:800]
        if approach:
            payload["approach"] = approach[:800]
        if confidence:
            payload["confidence"] = confidence[:800]
        assumptions = clean_list(written.get("assumptions"))
        limitations = clean_list(written.get("limitations"))
        what_would_change = clean_list(written.get("what_would_change"))
        if assumptions:
            payload["assumptions"] = assumptions
        if limitations:
            payload["limitations"] = limitations
        if what_would_change:
            payload["what_would_change"] = what_would_change
    return payload


def has_renderable_content(payload: Dict[str, Any]) -> bool:
    """True when the payload holds enough for a panel to be worth a toggle at all."""
    if not isinstance(payload, dict):
        return False
    if any(payload.get(k) for k in ("understood", "approach", "confidence",
                                    "assumptions", "limitations", "what_would_change")):
        return True
    used = payload.get("used") or {}
    return bool(used.get("cards") or used.get("sources") or used.get("reads")
                or used.get("actions") or used.get("web"))
