"""BASELINE in-situ arm: the same deep-run prompt and headless, tool-less path as ``insitu_standing_instructions``
with the standing-instructions layer emptied, so the difference between the two arms is the layer alone."""
import os

import common  # noqa: F401  (sets sys.path for the repo)
from quest_ai_runner.core.goal_runner_contract import compose_goal_prompt

from esoi_behavior import insitu
from esoi_behavior.judge import build_judge


def build(meter):
    run = insitu.build_runner(meter, os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def respond(case):
        return run(insitu.compose(case, compose_goal_prompt, standing=False))
    respond.judge = build_judge(meter)
    return respond
