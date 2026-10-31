"""Rule Q4 (scripts/esoi_rules.py): after the model-free first stage a metric's cascade has a `one` stage, a single real case."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("qar_esoi_rules_q4", ROOT / "scripts" / "esoi_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

OFFLINE = "offline [sample=full | real=no | gate=all-pass | when=no model]"
ONE = "one [sample=sample:1 | real=yes | gate=all-pass | when=a single real case runs end to end and is hand-read]"


def run(cells, waived=()):
    metrics = [dict(metric_id=f"m{i}", cascading_conditionals=cell) for i, cell in enumerate(cells)]
    return rules.single_case_problems(metrics, lambda metric_id: metric_id in waived)


def test_one_right_after_the_model_free_stage_passes():
    bad, counts = run([f"{OFFLINE} > {ONE} > three [sample=sample:3 | real=yes]"])
    assert bad == [] and counts == (1, 1, 0)


def test_a_cascade_without_a_second_stage_fails():
    bad, counts = run([OFFLINE])
    assert len(bad) == 1 and bad[0].startswith("Q4 metrics.csv m0: it has no second stage") and counts == (1, 0, 0)


def test_a_larger_sample_straight_after_the_floor_fails():
    bad, _ = run([f"{OFFLINE} > three [sample=sample:3 | real=yes]"])
    assert "its second stage is three" in bad[0]


def test_one_that_is_not_a_single_real_case_fails():
    bad, _ = run([f"{OFFLINE} > one [sample=sample:3 | real=yes]", f"{OFFLINE} > one [sample=sample:1 | real=no]"])
    assert len(bad) == 2


def test_a_waiver_skips_a_metric_and_is_counted():
    bad, counts = run([OFFLINE, f"{OFFLINE} > {ONE}"], waived={"m0"})
    assert bad == [] and counts == (2, 1, 1)


def test_a_metric_with_no_cascade_is_left_to_q2():
    bad, counts = run([""])
    assert bad == [] and counts == (0, 0, 0)
