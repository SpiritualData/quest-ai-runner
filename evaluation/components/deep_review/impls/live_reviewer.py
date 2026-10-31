"""LIVE ARM: the production reviewer. ``DEEP_REVIEW_PROMPT`` is filled exactly as ``review_liveness`` fills
it and sent through ``SubprocessGoalRunner.call_reviewer_cli`` (one-shot ``claude -p --model <review model>``),
then read by ``parse_deep_review``. Needs the ``claude`` CLI on PATH; it cannot run, and so cannot be
recorded, on a host without it (waivers.csv S10 run-deep-work:unmeasured). Makes 39 small calls. Run it with
--record where the CLI exists."""
import shutil

from quest_ai_runner.core.goal_runner import (
    DEEP_REVIEW_PROMPT, SubprocessConfig, SubprocessGoalRunner, parse_deep_review)


def build(meter):
    if shutil.which("claude") is None:
        raise SystemExit("live_reviewer needs the claude CLI on PATH; use --impl rule_floor offline")
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/tmp", claude_path="claude"))

    def review(elapsed, idle, tail):
        meter.record()
        verdict = parse_deep_review(runner.call_reviewer_cli(
            DEEP_REVIEW_PROMPT.format(elapsed=elapsed, idle=idle, tail=tail)))
        return {"stop": None, "reason": "unreadable verdict"} if verdict is None else {"stop": verdict[0], "reason": verdict[1]}
    return review
