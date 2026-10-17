"""Score one mid-run input case with the shared declarative check (``expect.py``); ``aggregate`` reports
pass counts per kind."""
import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
