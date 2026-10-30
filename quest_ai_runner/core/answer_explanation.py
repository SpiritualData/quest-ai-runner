"""The USER-FACING account of how a turn reached its answer ("Explain how I got this").

This is NOT the internal reasoning narration. That channel relayed the run's OWN wording (plan
rationales, "Understood as: ...", partial narration beats) and was deliberately removed from what
the reader sees. What this module produces is different in kind: wording composed FOR THE READER,
about the run, plus a genuine RECORD of what the run actually touched.

The split matters, so it is encoded in the payload shape rather than left to a convention:

  * ``used``, ``signals`` and (version 3) ``metrics`` are FACTS. They are assembled here from the
    turn's real trace (the observations the gather steps produced, the execution record, the
    assembled cards and sources, the goal verdict, the exit reason, and the metric results the
    runner's own ledger kept for the turn's deep goals). No model writes them. They can be checked.
  * ``understood``, ``approach``, ``assumptions``, ``confidence``, ``limitations``,
    ``what_would_change`` and ``evolvable_structure`` are a model-written summary, constrained to
    that same trace. They are a reconstruction, not a transcript of anything, which is why the
    consumer shows a disclaimer.

``evolvable_structure`` (version 2) says how Evolvable Structure that Optimizes Intent (ESOI, the
runner's standing instructions, ``prompts/evolvable_structure.md``) shaped the answer, in the ESOI
intent vocabulary: the ``ideal`` the answer serves, a practical ``metric`` the reader can check it
by, and the ``structure`` it built on or left open to change. ``what_would_change`` is the evolve
half of the same section (what would change or improve the answer next time); it keeps its version
1 key so a version 1 consumer still renders it.

``metrics`` (version 3) is what the relevant metric was before and after the turn's change, one entry
per metric, copied from ``OrchestratorResult.metric_results`` (shape and meaning: ``docs/metric-results.md``),
with ``metric_missing`` when a change ended with no metric result recorded. The values come from the
runner's ledger and meters, so the model-written half never touches them: it is told the values are
shown separately and must not state them, and the record it is constrained to carries them so it cannot
contradict them.

Three invariants this module exists to hold:

1. **Eligibility is model-free.** ``is_eligible`` is a boolean over the trace: reads happened, or
   actions executed, or a deep run happened, or context reached the answer, or the planner took
   more than one step, or the web was searched, or the turn has metric results. It costs nothing,
   needs no threshold tuning, and cannot itself be wrong in an expensive way. A plain "Hi" takes the
   small-talk short circuit, gathers nothing, answers at step 0, and so is ineligible for free: no
   event, no toggle.
2. **The explanation may never assert a step the record does not show.** The generation prompt is
   given the execution record and told to describe only what is in it. This is the same failure the
   goal verifier already guards with ``claims_unexecuted``: a confident account of tool calls that
   never happened is worse than no account at all.
3. **The facts survive the model.** When the generation call fails, cannot run or returns nothing
   usable, the payload is still built from the trace alone, ``metrics`` included. No model call is
   added for them and none can change them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from esoi.metric_results import SOURCE_MEASURED, SOURCE_NOT_APPLICABLE, SOURCE_RECORDED, SOURCES, format_value

from .prompt_specs import PromptSpec, register

log = logging.getLogger(__name__)

# The payload version a consumer can branch on if the shape ever grows. Version 2 added the optional
# ``evolvable_structure`` object; version 3 added the optional ``metrics`` list and ``metric_missing``
# flag (facts, written by code). Every earlier key is unchanged, so an older consumer is safe.
EXPLANATION_VERSION = 3

# The ``evolvable_structure`` keys, each one optional sentence.
EVOLVABLE_KEYS = ("ideal", "metric", "structure")

# How many items of any one kind survive into the payload. The panel is a summary, not a log, and
# the payload is persisted on the message, so it stays small on purpose.
MAX_ITEMS_PER_KIND = 12

# The keys of one ``metrics`` entry: exactly ``MetricResult.to_dict()`` (``docs/metric-results.md``).
METRIC_KEYS = ("solution_id", "metric_id", "name", "milestone", "before", "after", "delta", "source", "reason",
                "disagrees")
METRIC_NUMBER_KEYS = ("before", "after", "delta")
METRIC_TEXT_CHARS = 300

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
                "description": "1-2 sentences to 'you': what their question was taken to be. Plain "
                               "language, no ids, file names, or internal vocabulary.",
            },
            "approach": {
                "type": "string",
                "description": "1-2 sentences naming HOW the answer was worked out (e.g. compared "
                               "options, calculated from figures found, ruled out alternatives). "
                               "The shape of the reasoning, not the tools.",
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 unstated assumptions that, if wrong, would change the "
                               "answer. Skip ones that would hold for any answer. Empty if none.",
            },
            "confidence": {
                "type": "string",
                "description": "One sentence: how confident, and why, grounded in what was checked "
                               "this turn. Not 'fully confident' unless the record shows a check.",
            },
            "limitations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 concrete limits (not checked, missing, thin). Empty if none.",
            },
            "what_would_change": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 specific things that would change this answer or improve "
                               "the next one: a fact that, if different, would change it, or what "
                               "they could tell you, check or measure next time. Do not restate an "
                               "assumption or limitation already listed.",
            },
            "evolvable_structure": {
                "type": "object",
                "description": "How the answer serves their intent and can improve over time. Omit "
                               "for a simple factual reply where it would only restate the answer.",
                "properties": {
                    "ideal": {
                        "type": "string",
                        "description": "One sentence: the outcome a good answer serves for them, "
                                       "the purpose behind the question, not a restatement of it.",
                    },
                    "metric": {
                        "type": "string",
                        "description": "One sentence: a practical way they can tell whether the "
                                       "answer is achieving that outcome (something to observe "
                                       "or check).",
                    },
                    "structure": {
                        "type": "string",
                        "description": "One sentence: the lasting thing the answer created, changed "
                                       "or built on (a plan, habit, goal, document, earlier "
                                       "conversation) and what stays open to change. Empty when "
                                       "nothing lasting is involved.",
                    },
                },
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
- evolvable_structure: Quest AI works by Evolvable Structure that Optimizes Intent, turning what a
  person wants into something that serves it and can improve over time. Name the ideal the answer
  serves and a practical way to check it; name a structure only if the answer or the record shows
  one (if the answer says how it applied evolvable structure, draw on that). Leave it out for a
  simple factual reply.
- Never use an em dash. Split the sentence, or use a comma, colon, or parentheses.{metric_rule}
{language}"""

EXPLAIN_SPEC = register(PromptSpec(
    id="explain-an-answer.how-i-got-this", solution="explain-an-answer",
    metric="metric-explain-an-answer", tier="fast", cache_layer="tail",
    inputs=("user_message", "goal_condition", "answer", "record", "metric_rule", "language"),
    template=EXPLAIN_PROMPT, output_schema=EXPLAIN_TOOL,
))

# Added to the hard rules only for a turn that has metric results (``metric_rule``); a turn without any
# gets the prompt it always got. The values are facts the reader sees from the record, so the prose must
# not carry numbers that could differ from them.
METRIC_RULE = (
    "\n- Metric results: where the record lists metric results (or says none was recorded), the runner's code "
    "kept them, and they are shown to the reader separately, straight from the record. Do NOT state, round, "
    "compare or paraphrase any figure for them, and do not say a metric improved or got worse. In "
    "evolvable_structure.metric, say what to watch or check next time, without numbers for these results.")


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
    # The turn's metric results as ``MetricResult.to_dict()`` entries, and the flag set when a change
    # ended with none recorded (``OrchestratorResult.metric_results`` / ``.metric_missing``). Kept by
    # the runner's code, never by a model.
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    metric_missing: bool = False

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
    metrics = getattr(result, "metric_results", None)
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
        metrics=list(metrics) if isinstance(metrics, list) else [],
        metric_missing=getattr(result, "metric_missing", False) is True,
    )


def is_eligible(trace: TurnTrace) -> bool:
    """Is this turn worth explaining? A boolean over the real trace. No model call, no threshold.

    True when the run did something a person could reasonably want accounted for: it read or
    searched, it executed an action, it ran deep, it answered on assembled context, it took more
    than one planning step, it went to the web, or the runner kept a metric result for it (or noted
    that a change ended with none). False otherwise, which is exactly the plain small-talk case (no
    retrieval, no actions, answered at the first step).

    Only answer and deep turns are ever eligible. A confirm, a clarification and a cancelled run
    are not answers, so there is nothing to explain.
    """
    if trace.kind not in ("answer", "deep"):
        return False
    if not (trace.answer or "").strip():
        return False
    if trace.kind == "deep":
        return True
    if render_metrics(trace):
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
    so in words, because that is the case where a model is most tempted to invent a process. A turn's
    metric facts come first, so a long record that is cut to fit the prompt loses its tail, not them.
    """
    lines: List[str] = metric_record_lines(trace)
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


# How the record tells the generation call who vouches for a metric entry, in plain words.
METRIC_SOURCE_WORDS = {SOURCE_MEASURED: "measured by the runner's own meter",
                       SOURCE_RECORDED: "recorded by the AI, not checked"}


def metric_record_lines(trace: TurnTrace) -> List[str]:
    """The metric facts as record lines, so the written half cannot contradict them. ``[]`` for a turn without any.

    The values are here on purpose: the model is told not to state them (``METRIC_RULE``), and a model that
    knows them is less likely to write a figure of its own that differs."""
    facts = render_metrics(trace)
    entries = facts.get("metrics") or []
    lines: List[str] = []
    if entries:
        lines.append("Metric results the runner's code kept for this change (shown to the reader separately):")
    for entry in entries:
        if entry["source"] == SOURCE_NOT_APPLICABLE:
            lines.append(f"  - no metric applies: {entry['reason']}")
            continue
        label = entry["name"] or entry["metric_id"] or "a metric"
        note = "; the AI's record and the runner's reading differ" if entry["disagrees"] else ""
        lines.append(f"  - {label}: before {format_value(entry['before'])}, after {format_value(entry['after'])} "
                     f"({METRIC_SOURCE_WORDS[entry['source']]}{note})")
    if facts.get("metric_missing"):
        lines.append("No metric result was recorded for this change.")
    return lines


def metric_rule(trace: TurnTrace) -> str:
    """The prompt's metric rule for a turn that has metric facts, ``""`` (changing nothing) for one that has none."""
    return METRIC_RULE if render_metrics(trace) else ""


def finite_number(value: Any) -> Optional[Any]:
    """``value`` when it is an int or float that is finite (a bool and a string are not numbers), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def clean_metric(entry: Any) -> Optional[Dict[str, Any]]:
    """One metric entry as the payload carries it: the ten keys of ``MetricResult.to_dict()``. Never raises.

    Numbers stay numbers and anything else becomes ``None``; text is capped; an entry that is not a dict or whose
    ``source`` is not one the ledger writes is dropped, because nobody could vouch for it."""
    if not isinstance(entry, dict) or entry.get("source") not in SOURCES:
        return None
    out: Dict[str, Any] = {}
    for key in METRIC_KEYS:
        value = entry.get(key)
        if key in METRIC_NUMBER_KEYS:
            out[key] = finite_number(value)
        elif key == "disagrees":
            out[key] = value is True
        else:
            out[key] = str(value or "").strip()[:METRIC_TEXT_CHARS]
    return out


def render_metrics(trace: TurnTrace) -> Dict[str, Any]:
    """The ``metrics`` section: what the metric was before and after the change, from the trace's ledger rows only.

    ``{"metrics": [...]}`` (at most ``MAX_ITEMS_PER_KIND`` entries) when the turn has any, plus
    ``"metric_missing": True`` when a change ended with no metric result recorded. ``{}`` for a turn with neither,
    so such a turn's payload gets no new key. No model writes or edits any of it."""
    entries = [e for e in (clean_metric(raw) for raw in (trace.metrics or [])) if e][:MAX_ITEMS_PER_KIND]
    out: Dict[str, Any] = {}
    if entries:
        out["metrics"] = entries
    if trace.metric_missing:
        out["metric_missing"] = True
    return out


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


# A deep turn's outcome when it carries no goal verdict of its own: the deep runner's verifier
# already decided it, and ``orchestrator_run_answer`` records that as the exit reason.
DEEP_EXIT_MET = {"deep_met": True, "deep_not_met": False}


def render_signals(trace: TurnTrace) -> Dict[str, Any]:
    """The ``signals`` section: the run's own outcome facts, for the confidence block."""
    verdict = trace.goal_verdict or {}
    met = verdict.get("met")
    if met is None:
        met = DEEP_EXIT_MET.get(trace.exit_reason or "")
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
    stand on their own. The consumer simply has fewer prose sections to render. The same holds for
    ``metrics``: it comes from the trace alone, and a ``metrics`` key a model put in ``written`` is
    never read.
    """
    payload: Dict[str, Any] = {
        "version": EXPLANATION_VERSION,
        "used": render_used(trace),
        "signals": render_signals(trace),
        **render_metrics(trace),
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
        evolvable = clean_evolvable(written.get("evolvable_structure"))
        if evolvable:
            payload["evolvable_structure"] = evolvable
    return payload


def clean_evolvable(raw: Any) -> Dict[str, str]:
    """Keep the non-empty ``evolvable_structure`` sentences, each capped. Never raises."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key in EVOLVABLE_KEYS:
        text = str(raw.get(key) or "").strip()
        if text:
            out[key] = text[:400]
    return out


def has_renderable_content(payload: Dict[str, Any]) -> bool:
    """True when the payload holds enough for a panel to be worth a toggle at all."""
    if not isinstance(payload, dict):
        return False
    if any(payload.get(k) for k in ("understood", "approach", "confidence", "assumptions",
                                    "limitations", "what_would_change", "evolvable_structure")):
        return True
    if payload.get("metrics") or payload.get("metric_missing"):
        return True
    used = payload.get("used") or {}
    return bool(used.get("cards") or used.get("sources") or used.get("reads")
                or used.get("actions") or used.get("web"))
