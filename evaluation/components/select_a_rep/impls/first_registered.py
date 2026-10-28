"""BASELINE: the floor. Always the first representative in the registry, whatever the task says."""


def build(meter):  # noqa: ARG001
    def select(case):
        reg = case["registry"]
        if not reg:
            return {"rep_id": None}
        first = next(iter(reg.items()))
        value = first[1]
        return {"rep_id": (value.get("rep_id") or first[0]) if isinstance(value, dict) else first[0]}
    return select
