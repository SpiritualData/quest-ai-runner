"""The runner picks the Solution an autopilot run iterates and puts it in the brief (runner/autopilot_esoi.py)."""
from quest_ai_runner.runner import autopilot_esoi as esoi
from quest_ai_runner.runner.autopilot_briefs import compose_batch_text


def sol(rid, name, metrics=None, milestones="M1 do it"):
    return {"type": "solution", "recordId": rid,
            "fields": {"name": name, "milestones": milestones, "metric_ids": metrics or []}}


def met(rid, record, at="2026-10-20T00:00:00+00:00", solution_ids=None):
    return {"type": "metric", "recordId": rid, "fieldMeta": {"record": {"at": at}},
            "fields": {"name": f"metric {rid}", "record": record, "command": "count rows",
                       "solution_ids": solution_ids or []}}


PLAN = {"type": "solution", "recordId": "derived:q:solution:plan", "fields": {"name": "Plan"}}


def test_derived_plan_is_never_picked():
    assert esoi.pick_solution([PLAN]) is None


def test_gap_first_then_unmeasured_then_oldest():
    records = [PLAN, sol("s1", "A", ["m1"]), sol("s2", "B", ["m2"]), sol("s3", "C"),
               met("m1", "5 (2026-10-26)", "2026-10-26T00:00:00+00:00"),
               met("m2", "7 (2026-10-20)", "2026-10-20T00:00:00+00:00")]
    assert esoi.pick_solution(records)[0]["recordId"] == "s3"          # no metric at all
    records = [r for r in records if r["recordId"] != "s3"]
    assert esoi.pick_solution(records)[0]["recordId"] == "s2"          # measured longest ago
    records.append(sol("s4", "D", ["m4"]))
    records.append(met("m4", "Not yet measured (2026-10-23).", "2026-10-23T00:00:00+00:00"))
    picked = esoi.pick_solution(records)
    assert picked[0]["recordId"] == "s4" and "never measured" in picked[2]


def test_metric_linked_from_the_metric_side_and_string_ids():
    records = [sol("s1", "A", "['m1']"), sol("s2", "B"), met("m2", "3", solution_ids="['s2']"),
               met("m1", "4", "2026-10-01T00:00:00+00:00")]
    assert esoi.pick_solution(records)[0]["recordId"] == "s1"


def test_thin_spec_reports_gaps_and_refreshes_once():
    calls = []

    class Client:
        def get_spec(self, t, i):
            return {"records": [PLAN, sol("s1", "Legacy strategy", milestones="")]}

        def refresh_spec(self, t, i, reason="manual", force=False):
            calls.append(reason)

    text = esoi.esoi_readiness(Client(), "quest_x")
    assert calls == ["autopilot"]
    assert "no Metric record at all" in text and "quest note" in text and "Legacy strategy" in text and "none written" in text


def test_full_spec_does_not_refresh_and_names_metric():
    class Client:
        def get_spec(self, t, i):
            return {"records": [sol("s1", "P1", ["m1"]), met("m1", "64.7% (2026-10-27)")]}

        def refresh_spec(self, *a, **k):
            raise AssertionError("a complete specification is not refreshed")

    text = esoi.esoi_readiness(Client(), "quest_x")
    assert "Iterate this Solution: P1" in text and "64.7%" in text and "record_metric_result" in text


def test_unavailable_client_gives_none():
    class Boom:
        def get_spec(self, *a):
            raise RuntimeError("down")

    assert esoi.esoi_readiness(object(), "q") is None
    assert esoi.esoi_readiness(Boom(), "q") is None


def test_block_rides_in_every_batch_brief():
    text = compose_batch_text("Outcome", "Zee", esoi_readiness="ESOI READINESS pick S1")
    assert "ESOI READINESS pick S1" in text and text.index("ESOI READINESS") < text.index("IMPROVE ONE SOLUTION")


def test_solution_with_milestone_beats_bare_strategy_without_metric():
    records = [sol("user_1", "Bare strategy", milestones=""), sol("s1", "P1", ["m1"]), met("m1", "4 (2026-10-26)")]
    assert esoi.pick_solution(records)[0]["recordId"] == "s1"


def test_the_readiness_block_tells_the_run_to_follow_the_metrics_cascade():
    records = [sol("s1", "A", ["m1"]), met("m1", "5 (2026-10-26)", "2026-10-26T00:00:00+00:00")]
    text = esoi.render({"records": records}, esoi.pick_solution(records), refreshed=False)
    assert "cheapest cascading stage first" in text and "a stage not run stays empty" in text
    assert "—" not in text
