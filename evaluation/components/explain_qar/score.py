"""Score one documentation case with the declarative ``expect`` (see ``expect.py``); ``aggregate`` reports per kind."""
import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
