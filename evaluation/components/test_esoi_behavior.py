"""Offline tests for the ESOI behaviour dataset: well-formed, covers all 13 principles, and its checks
tell the reference good reply from the reference bad one. No model call."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from esoi_behavior import score  # noqa: E402

CASES = common.load_cases("esoi_behavior")


def test_dataset_is_well_formed_and_covers_every_principle():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) and 30 <= len(ids) <= 60
    primary = {c["principle"].split(";")[0] for c in CASES}
    assert primary == {f"P{n}" for n in range(1, 14)}
    for c in CASES:
        assert c["scenario"].strip() and c["expected_behavior"].strip() and c["must_have"].strip(), c["id"]
        assert "—" not in " ".join(str(v) for v in c.values()), f"{c['id']} has an em dash"
        for p in score._patterns(c["must_have"]) + score._patterns(c["must_not"]):
            re.compile(p)
    assert any(c["heldout"] for c in CASES)


def test_checks_accept_the_good_reference_and_reject_the_bad_one():
    for c in CASES:
        assert not score.check(c["good_reference"], c), (c["id"], score.check(c["good_reference"], c))
        assert score.check(c["bad_reference"], c), f"{c['id']}: the bad reference passes the checks"


def test_run_component_offline_arms(tmp_path):
    for impl, expect_pass in (("reference_good", len(CASES)), ("reference_bad", 0)):
        fn = common.load_impl(impl, "esoi_behavior")(common.CallMeter(0))
        rows = [score.score_case(fn, c) for c in CASES]
        assert sum(r["ok"] for r in rows) == expect_pass
