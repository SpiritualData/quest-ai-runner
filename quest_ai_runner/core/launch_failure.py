"""Why a worker never started, in plain words: the one wording the status line and the terminal's cause block share.

A runner that could not start the work says so in ``DeepResult.launch_failed`` and, when it knows it from the process outcome
itself, ``launch_problem`` (never from text). The runner's own sentence (``DeepResult.error``) opens with
``LAUNCH_FAILED_PREFIX`` and is left as it is, because the quest mailer keys on those opening words; what a person reads is
the cause that follows, worded here.
"""
from __future__ import annotations

from typing import Any, Optional

from .adapters_values import LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED

# The words every runner starts the error of a worker that never started with (``DeepResult.error``).
LAUNCH_FAILED_PREFIX = "The deep worker could not start"
# What a person reads for the two causes a runner reports as a structured ``launch_problem``.
PROBLEM_TEXT = {
    LAUNCH_MISSING_PROGRAM: "Claude Code (the claude program) isn't installed on this machine, or the terminal can't find it.",
    LAUNCH_PERMISSION_DENIED: "Claude Code (the claude program) could not be run, because permission was denied.",
}
# The longest cause shown from a runner's own words.
CAUSE_CHARS = 240


def cause(error: Optional[str], problem: str = "") -> str:
    """Why a worker could not start: plain words for a structured ``launch_problem``, else the
    runner's own words minus its stock opening phrase."""
    if problem in PROBLEM_TEXT:
        return PROBLEM_TEXT[problem]
    text = " ".join(str(error or "").split())
    if text.startswith(LAUNCH_FAILED_PREFIX):
        text = text[len(LAUNCH_FAILED_PREFIX):].lstrip(": ").strip()
    text = text[:CAUSE_CHARS].rstrip()
    return text or "no reason was given"


def status_text(result: Any) -> str:
    """The status line for a worker that never started: ``Could not start the work: <cause>.``"""
    why = cause(getattr(result, "error", None), getattr(result, "launch_problem", "") or "")
    return f"Could not start the work: {why}" + ("" if why.endswith((".", "!", "?")) else ".")
