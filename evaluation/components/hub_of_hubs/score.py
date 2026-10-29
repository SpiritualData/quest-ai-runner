"""Scoring is the hub's (``hubofhubs.score``); this module only hands it QAR's bindings."""
from hubofhubs import evaluate, score as hub_score

from hub_of_hubs.binding import binding


def score_case(fn, case):
    return hub_score.score_case(fn, case)


def aggregate(rows):
    facts, connections, _numbers = evaluate(binding())
    return hub_score.aggregate(rows, facts, connections)
