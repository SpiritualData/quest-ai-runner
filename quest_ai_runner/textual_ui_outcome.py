"""How a turn ended, in plain words: the one closing line, why work could not start, a failed model call.

Pure functions with no Textual in them, so the wording is testable on its own and every consumer of
the terminal reads the outcome the same way. Every decision here reads a structured field
(``DeepResult.met``, ``launch_failed``, ``usage_limited``, ``paused``, ``decision_id``,
``OrchestratorResult.kind``), never the words of a model's output (hard rule #3).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from .core import launch_failure, provider_outage, usage_limit

# The most passes one request gets without the person asking again: their own, plus one automatic
# second pass when a plan was written and nothing ran.
MAX_AUTO_PASSES = 2

# How a turn ended (``turn_outcome``).
OUTCOME_FINISHED = "finished"          # every goal was met
OUTCOME_STOPPED = "stopped"            # the work ran and did not finish
OUTCOME_NOT_STARTED = "not_started"    # the worker could not start; nothing ran
OUTCOME_NOT_RUN = "not_run"            # a plan, but no worker was run at all
OUTCOME_LIMIT = "limit"                # paused at the subscription's usage limit
OUTCOME_WAITING = "waiting"            # waiting for the person's answer


def speaker_label(rep_name: str) -> str:
    """``Quest AI:`` for a rep whose name already says it is an AI, else ``Zee (AI):``.

    A name that already contains the word AI needs no second one; a person's name does, so nobody
    reads a reply as the person's own words."""
    name = (rep_name or "").strip() or "Assistant"
    return f"{name}:" if re.search(r"\bAI\b", name) else f"{name} (AI):"


def deep_results(final: Any) -> List[Any]:
    return list(getattr(final, "deep_results", None) or [])


def waits_for_person(final: Any) -> bool:
    """True when the turn ended on a question or an approval only the person can answer."""
    if getattr(final, "kind", None) == "confirm" or getattr(final, "decision_id", None):
        return True
    return any(getattr(d, "decision_id", None) for d in deep_results(final))


def turn_outcome(final: Any) -> Optional[str]:
    """How the turn ended, or None for a plain answer (which has no outcome to report).

    A turn waiting for the person wins over everything else: that is what they have to act on. A run
    the person paused or stopped, with a session to continue, is worded by the terminal's own
    pause and cancel lines (``_settle_run_control``) and never reaches here; any other run that did
    not finish is "stopped before finishing"."""
    if waits_for_person(final):
        return OUTCOME_WAITING
    if getattr(final, "kind", None) != "deep":
        return None
    results = deep_results(final)
    if any(getattr(d, "usage_limited", False) for d in results):
        return OUTCOME_LIMIT
    if any(getattr(d, "launch_failed", False) for d in results):
        return OUTCOME_NOT_STARTED
    if not results:
        return OUTCOME_NOT_RUN
    if all(getattr(d, "met", False) for d in results):
        return OUTCOME_FINISHED
    return OUTCOME_STOPPED


def allows_second_pass(final: Any) -> bool:
    """False when something other than "nothing ran" is why the work did not run.

    A worker that could not start would fail the same way again, a usage limit is refused until it
    resets, a question waits for its answer, and a pause or a cancel was the person's own choice."""
    if waits_for_person(final):
        return False
    return not any(getattr(d, flag, False) for d in deep_results(final)
                   for flag in ("launch_failed", "usage_limited", "paused", "cancelled"))


def closing_text(outcome: str, reset: str = "") -> Tuple[str, str]:
    """The (text, style) that opens the closing line for ``outcome``. ``reset`` is the reset clause
    (``usage_limit.reset_clause``), empty when the limit did not say when."""
    if outcome == OUTCOME_FINISHED:
        return "✓ Done", "green"
    if outcome == OUTCOME_STOPPED:
        return "~ Stopped before finishing", "yellow"
    if outcome == OUTCOME_NOT_STARTED:
        return "✗ Nothing ran", "yellow"
    if outcome == OUTCOME_NOT_RUN:
        return "~ Nothing was run", "yellow"
    if outcome == OUTCOME_LIMIT:
        return usage_limit.pause_sentence(reset), "yellow"
    return "Waiting for your answer", "yellow"


def limit_reset(final: Any, now: Optional[datetime] = None) -> str:
    """The reset clause (``at 15:40``) of the usage limit a deep result stopped on, or ``""``.

    Read from the limit the run itself reported (the CLI's own refusal text in ``DeepResult.error``),
    else from the one the lane noted when it hit it. Never a time nobody reported."""
    for d in deep_results(final):
        if getattr(d, "usage_limited", False):
            found = usage_limit.detect_usage_limit(getattr(d, "error", None) or "", now)
            if found is not None and found.resets_at is not None:
                return usage_limit.reset_clause(found, now)
    return usage_limit.reset_clause(usage_limit.active_limit(now), now)


def launch_failure_lines(final: Any) -> List[str]:
    """What the person reads when the worker never started, or [] when it did (or it was not asked to)."""
    failed = [d for d in deep_results(final) if getattr(d, "launch_failed", False)]
    if not failed:
        return []
    return ["I couldn't start the work, so nothing was changed.",
            f"The reason: {launch_failure.cause(getattr(failed[0], 'error', None), getattr(failed[0], 'launch_problem', ''))}",
            "Fix that, then send your request again."]


def provider_error_lines(error: BaseException, *, message_restored: bool,
                         now: Optional[datetime] = None) -> List[str]:
    """A failed model call as two plain sentences: what happened, and the way forward.

    ``message_restored`` says the failed message is back in the prompt box, so Enter sends it again."""
    limit = usage_limit.from_exception(error)
    if limit is not None:
        when = usage_limit.reset_clause(limit, now)
        what = "Claude Code's usage limit is reached, so I couldn't answer."
        if when:
            what += f" It resets {when}."
        later = " after it resets"
    elif provider_outage.from_exception(error) is not None:
        what = "The AI service is busy right now, so I couldn't answer."
        later = " in a minute"
    else:
        from .adapters.retry_utils import format_provider_error
        detail = " ".join(format_provider_error(error).split())[:240].rstrip()
        what = f"I couldn't answer: {detail}" if detail else "I couldn't answer."
        later = ""
    if message_restored:
        again = f"Your message is back in the box: press Enter to try again{later}."
    else:
        again = f"Send your message again to try again{later}."
    return [what, again]


def model_text(labels: Sequence[Tuple[Optional[str], Optional[str]]], verbose: bool) -> str:
    """The model(s) a turn ran on, once each: the short alias (``sonnet``), or with ``-v`` the full
    ``sonnet (claude-sonnet-5-5)``. ``labels`` are (model, resolved model) pairs."""
    from .interactive_session_render import _model_display, _model_label
    shown = {(_model_display(model, resolved) if verbose else _model_label(model or resolved))
             for model, resolved in labels if model or resolved}
    return ", ".join(sorted(s for s in shown if s))
