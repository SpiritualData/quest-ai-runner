"""The system-integrity area: its id answers offline by the strict definition, and the audit reads QAR's brain and the hub's research."""
import quest_ai_runner
from esoi.entry import targets

from quest_ai_runner.the_intelligence.critical_thinking.response_quality.area import KeepingGeneric


def test_the_id_of_the_area_answers_offline():
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}["system-integrity"]
    audit = quest_ai_runner.run("system-integrity", example, environ={})
    assert audit["vendor_branches"] == [] and 0.0 <= audit["config_share"] <= 1.0


def test_the_audit_scans_the_brain_for_the_terms_and_reads_the_hubs_staleness_and_connection_share():
    area = quest_ai_runner.run(KeepingGeneric, environ={})
    audit = area.audit([])
    assert audit["vendor_branches"] == [] and isinstance(audit["stale_areas"], list) and 0.0 <= audit["config_share"] <= 1.0
    assert area.audit(["zzzzunknownvendor"])["vendor_branches"] == []


def test_the_run_audit_is_the_hubs_check_of_a_run_record():
    area = quest_ai_runner.run(KeepingGeneric, environ={})
    assert area.audit_run({"steps": [{"solution": "planner", "status": "pending"}]}) == ["step planner is pending and does not say why"]
    assert area.audit_run({"steps": [{"solution": "planner", "status": "done"}]}) == []
