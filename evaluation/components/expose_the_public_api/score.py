"""Score one public-surface case with the declarative ``expect`` (see ``expect.py``); ``aggregate`` reports
the pass count per kind so a weak area of the surface shows in the headline."""
from esoi import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
