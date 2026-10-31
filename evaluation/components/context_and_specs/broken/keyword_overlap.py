"""BASELINE (broken): what a first draft would write. Every card and record that shares a word with the request, in the
order they are stored, with no ranking, no gate, no links and no "no solution yet". It returns a crowd for most requests
and something for an unrelated one, so the cases must fail it. Zero model calls."""
from context_and_specs.world import world
from esoi.spec_index import tokens


def build(meter):  # noqa: ARG001
    w = world(vector=False)

    def search(task, tags):  # noqa: ARG001
        words = set(tokens(task))
        hits = [v.key for v in w.views if words & set(v.terms)]
        return {"items": hits, "linked": {}, "no_solution_yet": False, "calls": 0, "route": "overlap"}
    return search
