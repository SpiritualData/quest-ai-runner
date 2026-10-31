"""Selecting intent specifications from an ESOI specification index: the QAR judge prompt and model call, parallel batches, safe
failure. The stages themselves (lexical, fusion, the gate) are tested in the ESOI library, esoi.spec_search."""
import json
import threading
from pathlib import Path

import pytest

from esoi import spec_search

from quest_ai_runner.core import repo_specs, spec_selection as ss

INDEX = ss.load_spec_index(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "esoi_repo_spec_index.json")
RIVAL = "a rival implementation beats the selected one and nobody switches the default"
WANT = "prob-winner-not-adopted"


class Judge:
    def __init__(self, keep, fail=False):
        self.keep, self.fail, self.calls, self.threads, self.lock = set(keep), fail, 0, set(), threading.Lock()

    def answer(self, messages, *, model, system=None, layers=None):
        with self.lock:
            self.calls += 1
            self.threads.add(threading.get_ident())
        if self.fail:
            raise RuntimeError("down")
        return json.dumps({"ids": [i for i in self.keep if f" {i}:" in messages[0]["content"]] + ["not-offered"]})


def test_the_index_loader_refuses_what_it_does_not_understand(tmp_path):
    bad = tmp_path / "i.json"
    bad.write_text(json.dumps({"version": 99, "entries": []}))
    with pytest.raises(ValueError):
        ss.load_spec_index(bad)
    assert ss.load_spec_index(INDEX)["n"] == INDEX["n"]


def test_the_model_chooses_in_parallel_batches_and_invented_ids_are_dropped():
    judge = Judge([WANT])
    got = ss.lexical_vector_llm(RIVAL, INDEX, provider=judge, model="fast", batch_size=4, k_lexical=12, expand=0)
    assert got.ids == [WANT] and got.calls == judge.calls == 3 and "not-offered" not in got.ids


def test_a_failed_model_call_keeps_the_candidates_and_a_failed_vector_search_changes_nothing():
    def broken(text, k):
        raise RuntimeError("store down")
    got = ss.lexical_vector_llm(RIVAL, INDEX, provider=Judge([], fail=True), model="fast", vector_search=broken, batch_size=50)
    assert WANT in got.ids and got.failed_batches == got.calls == 1


def test_the_vector_search_recovers_a_record_that_shares_no_word_with_the_request():
    want = "prob-evaluation-detached-from-shipped-code"
    task = "our dashboards went green yet customers still see errors"
    assert want not in [k for k, _ in spec_search.lexical_scored(spec_search.views_from_index(INDEX), task, 15, ss.CANDIDATE_KINDS)]
    got = ss.select_specs(task, INDEX, provider=Judge([want]), model="fast", vector_search=lambda text, k: [want, "not-in-the-index"])
    assert got.ids[0] == want and got.sources[want] == ["vector"]


def test_without_a_provider_the_fused_candidates_answer_and_links_are_marked():
    got = ss.select_specs(RIVAL, INDEX, expand=1)
    assert got.calls == 0 and WANT in got.ids
    assert all(src in got.ids for src in got.linked.values())


def test_the_metric_cases_name_records_that_exist_and_can_discriminate():
    """The real accuracy metric (evaluation/components/spec_selection) is only as good as its labels: every expected id is in the
    fixture index, the kinds include requests with other words and requests that concern nothing, and no arm is simulated."""
    base = Path(__file__).resolve().parent.parent / "evaluation" / "components" / "spec_selection"
    known = {e["id"] for e in json.loads((base / "fixture_index.json").read_text())["entries"]}
    cases = json.loads((base / "cases.json").read_text())
    for case in cases:
        assert set(case["expect"].get("has", {}).get("ids", [])) <= known, case["id"]
    assert {"direct", "paraphrase", "solution", "none"} <= {c["kind"] for c in cases}
    assert len(cases) >= 30
    for impl in (base / "impls").glob("*.py"):
        assert "SIMULATED = True" not in impl.read_text(), impl.name


def test_the_gated_selection_spends_one_call_on_an_ambiguous_request_and_none_otherwise():
    weak = lambda text, k: [(WANT, 0.60), ("prob-selection-default-kept-twice", 0.58)]   # noqa: E731
    judge = Judge([WANT])
    got = ss.lexical_vector_gated(RIVAL, INDEX, provider=judge, model="fast", vector_search=weak)
    assert got.route == "judged" and got.calls == judge.calls == 1 and WANT in got.ids
    strong = lambda text, k: [(WANT, 0.9)]   # noqa: E731
    judge = Judge([])
    got = ss.lexical_vector_gated(RIVAL, INDEX, provider=judge, model="fast", vector_search=strong)
    assert got.route == "sure" and judge.calls == 0 and got.ids[0] == WANT
    nothing = ss.lexical_vector_gated("zebra quokka marmalade", INDEX, provider=judge, model="fast", vector_search=lambda t, k: [("x", 0.1)])
    assert nothing.route == "nothing" and nothing.ids == [] and judge.calls == 0


def test_a_failed_judging_call_keeps_the_best_few_candidates():
    weak = lambda text, k: [(WANT, 0.60)]   # noqa: E731
    got = ss.lexical_vector_gated(RIVAL, INDEX, provider=Judge([], fail=True), model="fast", vector_search=weak, expand=0)
    assert got.failed_batches == 1 and 0 < len(got.ids) <= 3


def test_related_records_walks_links_nearest_first_and_intent_specs_lists_a_whole_intent():
    near = repo_specs.related_records(INDEX, WANT, depth=1)
    assert near and all(h["depth"] == 1 and h["via"] == WANT for h in near)
    far = repo_specs.related_records(INDEX, WANT, depth=2, limit=500)
    assert {h["id"] for h in near} < {h["id"] for h in far} and WANT not in {h["id"] for h in far}
    assert [h["depth"] for h in far] == sorted(h["depth"] for h in far)
    with pytest.raises(KeyError):
        repo_specs.related_records(INDEX, "no-such-record")
    everything = repo_specs.intent_records(INDEX)
    assert len(everything) == INDEX["n"] and everything[0]["kind"] == "intent" or everything[0]["kind"] == "problem"
    assert {h["id"] for h in repo_specs.intent_records(INDEX, INDEX["entries"][0]["intent"])} <= {e["id"] for e in INDEX["entries"]}


def test_implementations_are_reached_through_links_not_proposed_as_candidates():
    got = ss.select_specs("selected implementation default table", INDEX)
    assert all(INDEX_BY_ID[i]["kind"] != "implementation" for i in got.candidates)


INDEX_BY_ID = {e["id"]: e for e in INDEX["entries"]}
