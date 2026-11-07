"""ALTERNATIVE: the hub's single cheap classifier call. One model call per request through the loop's own cheap decision (Model Plumbing's ask for the offline orchestrator); nothing
else runs, no planning call, no tool, no worker. Paid: stage it with ``--sample N`` and ``--max-calls``."""
import os

import offline_orchestrator
from hubofhubs.api import SingleClassifier


class Plumbing:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def ask(self, prompt, tier=""):  # noqa: ARG002
        return self.orchestrator.cheap_decision(prompt)


def build(meter):
    arm = SingleClassifier(Plumbing(offline_orchestrator.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"))))
    return lambda kind, args: arm.route(args["request"], args.get("context") or None)["route"]
