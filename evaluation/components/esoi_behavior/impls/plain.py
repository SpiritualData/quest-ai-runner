"""ALTERNATIVE chat arm: the same model with no ESOI instructions, so the gain from the standing instructions is
a number. One model call per case plus the judge call; costs real money."""
import os

import common  # noqa: F401  (sets sys.path for the repo)
from esoi_behavior.judge import build_judge


def build(meter):
    from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider
    provider = meter.wrap(ClaudeCliProvider())
    model = os.environ.get("QAR_EVAL_MODEL", "haiku")

    def respond(case):
        return provider.answer([{"role": "user", "content": case["scenario"]}], model=model,
                               system="You are a helpful assistant.") or ""
    respond.judge = build_judge(meter)
    return respond
