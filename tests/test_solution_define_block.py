"""The deep brief when records exist but no solution passes the gate: the nearest solutions and the instruction to extend or define; no records stays empty."""
import pytest

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import solution_consideration


class Never:
    def answer(self, messages, model=None, **kwargs):
        raise AssertionError("no model call is made for this block")


class Root:
    def classify(self, text, limit=5, arm=""):
        return [{"solution_id": "a", "score": 0.1}, {"solution_id": "b", "score": 0.05}, {"solution_id": "c", "score": 0.01}]


def test_records_below_the_gate_get_the_define_block_with_the_two_nearest(monkeypatch, tmp_path):
    from hubofhubs.api import SolutionNetwork
    net = SolutionNetwork.from_rows({"solutions": [{"solution_id": s, "name": n, "purpose": p} for s, n, p in
                                                   (("a", "Alpha", "Does alpha work."), ("b", "Beta", "Does beta work."), ("c", "Gamma", "Does gamma work."))]})
    monkeypatch.setattr(solution_consideration, "chosen_for", lambda *a, **k: [])
    monkeypatch.setattr("quest_ai_runner.the_intelligence.critical_thinking.esoi_application.repo_specs.slice_root", lambda d: net)
    monkeypatch.setattr(net, "classify", lambda text, limit=5, arm="": [{"solution_id": s, "score": 0.1} for s in ("a", "b", "c")])
    monkeypatch.setattr(solution_consideration, "ask_for", lambda p: (lambda prompt: ""))
    block = solution_consideration.brief_block("unrelated request", str(tmp_path), object())
    assert "Alpha (a): Does alpha work." in block and "Beta (b)" in block and "Gamma" not in block
    assert "extend the nearest solution" in block and "define a new solution" in block and "one milestone" in block


def test_no_records_stays_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("QAR_SPEC_ROOT", raising=False)
    assert solution_consideration.brief_block("anything", str(tmp_path), Never()) == ""
