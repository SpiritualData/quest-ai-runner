"""CANDIDATE solution: the LIVE orchestrator with the ``route-a-message`` selection ``pre_route``: the cheap
pre-route (``core/pre_route.py``) settles a clear, non-destructive request for a ``direct`` tool on its own,
and every other message goes to the planner, which decides exactly as in ``qar_planner``. The meter counts
both calls where both ran."""
import os

import common
from classification.impls.qar_planner import decision_to_route


def build(meter):
    orch = common.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"),
                                             selections={"route-a-message": "pre_route"})

    def classify(message, context):
        quest = context["quest"]
        view = f"Quest: {quest['title']}\nOutcome: {quest['outcome']}"
        return decision_to_route(orch._plan(message, "", view, []))
    return classify
