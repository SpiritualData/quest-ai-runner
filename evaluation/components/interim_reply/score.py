"""Score one case against its labelled expectation (``esoi.expect``); ``aggregate`` reports passes per case kind."""
from esoi import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
