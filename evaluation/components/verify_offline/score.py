"""Score one offline-suite case with the declarative ``expect`` (see ``expect.py``); ``aggregate`` reports the
pass count per kind, and the shard cases also add up to the suite-wide collected count."""
import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
