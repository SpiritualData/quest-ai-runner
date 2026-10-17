"""LIVE in-situ arm: the case scenario as the TASK of QAR's real deep-run prompt (``compose_goal_prompt``, which
includes the shipped standing instructions as a required part), run through the headless ``claude -p`` path with
every tool disabled, then the model judge. See ``esoi_behavior/insitu.py`` for what is and is not reproduced.
Compare with ``insitu_plain``. Costs real money; run on a subset (``--only``) and with ``--max-calls``."""
import os

import common  # noqa: F401  (sets sys.path for the repo)
from quest_ai_runner.core.goal_runner_contract import compose_goal_prompt

from esoi_behavior import insitu
from esoi_behavior.judge import build_judge


def build(meter):
    run = insitu.build_runner(meter, os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def respond(case):
        return run(insitu.compose(case, compose_goal_prompt, standing=True))
    respond.judge = build_judge(meter)
    return respond
