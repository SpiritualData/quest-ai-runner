"""The keep-it-generic area: its id answers offline by the strict definition, and the audit reads QAR's brain and the hub's connections."""
import quest_ai_runner
from esoi.entry import targets

from quest_ai_runner.keep_it_generic.area import KeepingGeneric


def test_the_id_of_the_area_answers_offline():
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}["keep-it-generic"]
    audit = quest_ai_runner.run("keep-it-generic", example, environ={})
    assert audit["vendor_branches"] == [] and 0.0 <= audit["config_share"] <= 1.0


class Scorer:
    def metrics(self, facts, connections):
        return {"config_share": 0.5 if connections else 0.0}

    def evaluate(self, binding, folder=None, today=None):
        return None

    def check(self, fact, kind):
        return {}


def test_the_audit_scans_the_brain_for_the_terms_and_reads_the_share_through_the_scorer():
    area = quest_ai_runner.run(KeepingGeneric, environ={}, provided={"score-hub-connections": Scorer()})
    assert area.audit([]) == {"vendor_branches": [], "config_share": 0.5}
    assert area.audit(["zzzzunknownvendor"])["vendor_branches"] == []
