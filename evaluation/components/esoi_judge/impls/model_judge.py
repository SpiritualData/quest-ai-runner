"""The shipped ESOI model judge (``esoi_behavior/judge.py``) at the cheapest tier, graded on the reference replies.
One model call per reply."""
import common  # noqa: F401  (sets sys.path for the repo)
from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider

from esoi_behavior.judge import build_judge


def build(meter):
    judge = build_judge(meter, ClaudeCliProvider)

    def grade(case):
        passed, reason = judge(case["behavior_case"], case["reply"])
        return {"verdict": "pass" if passed else "fail", "reason": reason}
    return grade
