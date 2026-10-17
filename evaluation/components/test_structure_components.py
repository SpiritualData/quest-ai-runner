"""Offline pins for the infrastructure components: each live implementation passes a few of its own cases. The
broken baselines of these invariant components live in ``broken/`` and are failed by test_components.py. Whole case
files are run by ``run.py``, not here."""

import common
import pytest


def run(component, impl, ids):
    cases = [c for c in common.load_cases(component) if c["id"] in ids]
    fn = common.load_impl(impl, component)(common.CallMeter())
    score = __import__(f"{component}.score", fromlist=["score_case"]).score_case
    return {c["id"]: score(fn, c)["ok"] for c in cases}


@pytest.mark.parametrize("component,impl,ids", [
    ("check_the_structure", "live_checker", ["CTS-000", "CTS-004", "CTS-105"]),
    ("expose_the_public_api", "live_surface", ["API-001", "API-070", "API-100"]),
    ("learn_by_example", "live_examples", ["EXM-001", "EXM-014", "EXM-030"]),
    ("keep_secrets_out", "live_scanner", ["SEC-001", "SEC-013", "SEC-035"]),
    ("explain_qar", "shipped_docs", ["DOC-002", "DOC-300"]),
])
def test_live_implementation_passes_its_cases(component, impl, ids):
    assert all(run(component, impl, ids).values())

