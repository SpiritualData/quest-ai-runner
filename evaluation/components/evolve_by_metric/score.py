"""Score one evolve-by-metric case by the declarative ``expect`` of ``esoi.expect`` (no per-case branch in the scorer).
``aggregate`` reports pass counts per kind, so a switch the metric calls for and a switch it forbids read apart."""
from esoi import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
