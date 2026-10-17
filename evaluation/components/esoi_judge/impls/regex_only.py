"""BASELINE: the regex checks alone (all ``must_have`` match, no ``must_not``). Zero model calls. It agrees with
the labels by construction, because the same author wrote the references and the regexes; it is listed so the
judge's number has a comparator and the circularity is visible, not as evidence the regexes are right."""
import common  # noqa: F401  (sets sys.path for the repo)
from esoi_behavior import score


def build(meter):  # noqa: ARG001
    def grade(case):
        problems = score.check(case["reply"], case["behavior_case"])
        return {"verdict": "fail" if problems else "pass", "reason": "; ".join(problems)[:80]}
    return grade
