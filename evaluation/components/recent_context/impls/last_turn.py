"""FLOOR: ignores scope, fence and relevance and returns the cards of the most recent turn. What "always carry
the last turn forward" scores; the real gate must beat it on staleness and quest leaks."""
ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    def recall(case):
        return {"ids": [c["id"] for c in case["turns"][-1]["cards"]]}
    return recall
