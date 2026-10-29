"""BASELINE: what a first draft writes. Every request is reported as held and improved: a measured row of the case's starting
value both before and after, with no meter behind it, a reading even for a request with no metric, and a metric always
"proposed". It hides every regression."""


def build(meter):  # noqa: ARG001
    def hold(case):
        value = case["before"]
        return {"rows": [{"before": value, "after": value, "source": "measured"}], "missing": False, "proposed": True,
                "response": ""}
    return hold
