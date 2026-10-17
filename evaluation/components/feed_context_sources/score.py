"""Score one context-source case with the shared declarative check (``expect.py``); ``aggregate`` reports
pass counts per channel."""
import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
