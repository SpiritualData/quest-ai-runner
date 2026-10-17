"""ALTERNATIVE (validity check, not a candidate solution): returns each case's ``bad_reference``. It must
fail every case, or a check cannot tell naive behaviour from ESOI behaviour. Zero model calls."""
ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    return lambda case: case["bad_reference"]
