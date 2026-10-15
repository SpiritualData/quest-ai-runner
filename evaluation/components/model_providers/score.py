"""Score one model-provider case: run the kind, compare with the case's declarative ``expect`` (see
``expect.py``). ``aggregate`` reports pass counts per kind."""
import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
