"""ALTERNATIVE (validity check, not a candidate solution): returns each case's ``good_reference``. It must
pass every case, or a check is too strict for the behaviour it describes. Zero model calls."""
ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    return lambda case: case["good_reference"]
