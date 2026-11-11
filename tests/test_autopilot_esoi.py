"""The runner picks the Solution an autopilot run iterates and puts it in the brief (runner/autopilot_esoi.py)."""
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import autopilot_esoi as esoi
from quest_ai_runner.the_intelligence.proactivity.autopilot_briefs import compose_batch_text


def sol(rid, name, metrics=None, milestones="M1 do it"):
    return {"type": "solution", "recordId": rid,
            "fields": {"name": name, "milestones": milestones, "metric_ids": metrics or []}}


def met(rid, record, at="2026-10-20T00:00:00+00:00", solution_ids=None):
    return {"type": "metric", "recordId": rid, "fieldMeta": {"record": {"at": at}},
            "fields": {"name": f"metric {rid}", "record": record, "command": "count rows",
                       "solution_ids": solution_ids or []}}


PLAN = {"type": "solution", "recordId": "esr_plan_anchor", "derived": True, "fields": {"name": "Plan"}}


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


def test_a_proposed_solution_is_asked_for_by_priority_and_through_sub_intents():
    text = esoi.render({"records": []}, None, False)
    assert "priority order for the intent" in text and "never by appending" in text and "sub-intent" in text
    assert "—" not in text


def _two_areas():
    return [sol("esr_aaaaaaaa", "Area A", ["m1"]), sol("esr_bbbbbbbb", "Area B", ["m2"]), sol("esr_cccccccc", "Area C", ["m3"]),
            met("m1", "5", "2026-10-01T00:00:00+00:00"), met("m2", "7", "2026-10-20T00:00:00+00:00"),
            met("m3", "9", "2026-10-25T00:00:00+00:00")]


def test_a_character_iterates_only_the_solutions_its_instructions_name():
    read = ({"records": _two_areas()}, False)
    quest_wide = esoi.readiness_for(read, "Work the quest.")
    assert "Iterate this Solution: Area A" in quest_wide                      # no line: the oldest measured of all, as before
    mine = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: esr_bbbbbbbb, esr_cccccccc\nYou are the lead of B and C.")
    assert "Iterate this Solution: Area B (esr_bbbbbbbb)" in mine and "Area A" not in mine
    assert "Chosen among the Solutions your instructions name" in mine


def test_a_character_that_names_no_solution_iterates_none_and_an_unknown_id_falls_back():
    read = ({"records": _two_areas()}, False)
    none = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: none (weekly review of all solutions)\nReview.")
    assert "iterates none" in none and "Iterate this Solution" not in none
    unknown = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: esr_zzzzzzzz")
    assert "Iterate this Solution: Area A" in unknown and "not Solutions of this quest" in unknown
    assert esoi.named_solutions("no line here") is None and esoi.readiness_for(None, "x") is None


def impl(rid, solution_id, filepath):
    return {"type": "implementation", "recordId": rid, "fields": {"name": f"impl {rid}", "solution_id": solution_id, "filepath": filepath}}


def test_the_specification_links_a_character_to_its_solutions_and_wins_over_the_line():
    records = _two_areas() + [impl("i1", "esr_bbbbbbbb", "autopilot:rep_11111111, rep_22222222"), impl("i2", "esr_cccccccc", "autopilot:rep_22222222"),
                              impl("i3", "esr_aaaaaaaa", "docs/rep_11111111.md")]          # not an autopilot path: no link
    assert esoi.linked_solutions(records, "rep_11111111") == ["esr_bbbbbbbb"]
    assert esoi.linked_solutions(records, "rep_22222222") == ["esr_bbbbbbbb", "esr_cccccccc"]
    assert esoi.linked_solutions(records, "rep_33333333") == [] and esoi.linked_solutions(records, None) == []
    assert esoi.linked_solutions(_two_areas() + [impl("i4", ["esr_cccccccc"], "Autopilot:rep_11111111")], "rep_11111111") == ["esr_cccccccc"]   # a list cell
    read = ({"records": records}, False)
    mine = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: esr_bbbbbbbb", "rep_11111111")
    assert "Iterate this Solution: Area B (esr_bbbbbbbb)" in mine and "specification links to you" in mine and "different set" not in mine
    differs = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: esr_aaaaaaaa", "rep_22222222")
    assert "Iterate this Solution: Area B" in differs and "names a different set (esr_aaaaaaaa)" in differs
    no_line = esoi.readiness_for(read, "Work the quest.", "rep_22222222")
    assert "Iterate this Solution: Area B" in no_line and "different set" not in no_line


def test_a_character_the_specification_links_to_nothing_falls_back_to_its_instructions():
    read = ({"records": _two_areas() + [impl("i1", "esr_bbbbbbbb", "autopilot:rep_11111111")]}, False)
    reviewer = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: none (weekly review)", "rep_99999999")
    assert "iterates none" in reviewer
    by_line = esoi.readiness_for(read, "SOLUTIONS YOU ITERATE: esr_cccccccc", "rep_99999999")
    assert "Iterate this Solution: Area C" in by_line and "your instructions name" in by_line
