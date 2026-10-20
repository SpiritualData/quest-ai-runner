"""Selecting intent specifications from an ESOI specification index: narrowing, fusion, parallel batches, safe failure."""
import json
import threading
from pathlib import Path

import pytest

from quest_ai_runner.core import spec_selection as ss

INDEX = ss.load_spec_index(Path(__file__).resolve().parents[1] / "evaluation" / "components" / "spec_selection" / "fixture_index.json")
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


def test_lexical_narrowing_finds_the_record_and_no_match_returns_nothing():
    assert WANT in ss.narrow_lexical(INDEX, RIVAL, 5)
    assert ss.narrow_lexical(INDEX, "zebra quokka marmalade", 5) == []


def test_fusion_prefers_what_two_lists_agree_on():
    assert ss.fuse([["a", "b", "c"], ["c", "b", "d"]], 3) == ["c", "b", "a"]


def test_the_model_chooses_in_parallel_batches_and_invented_ids_are_dropped():
    judge = Judge([WANT])
    got = ss.select_specs(RIVAL, INDEX, provider=judge, model="fast", batch_size=4, k_lexical=12, expand=0)
    assert got.ids == [WANT] and got.calls == judge.calls == 3 and "not-offered" not in got.ids


def test_a_failed_model_call_keeps_the_candidates_and_a_failed_vector_search_changes_nothing():
    def broken(text, k):
        raise RuntimeError("store down")
    got = ss.select_specs(RIVAL, INDEX, provider=Judge([], fail=True), model="fast", vector_search=broken, batch_size=50)
    assert WANT in got.ids and got.failed_batches == got.calls == 1


def test_the_vector_search_recovers_a_record_that_shares_no_word_with_the_request():
    want = "prob-evaluation-detached-from-shipped-code"
    task = "our dashboards went green yet customers still see errors"
    assert want not in ss.narrow_lexical(INDEX, task, 15)
    got = ss.select_specs(task, INDEX, provider=Judge([want]), model="fast", vector_search=lambda text, k: [want, "not-in-the-index"])
    assert got.ids[0] == want and got.sources[want] == ["vector"]


def test_without_a_provider_the_fused_candidates_answer_and_links_are_marked():
    got = ss.select_specs(RIVAL, INDEX, expand=1)
    assert got.calls == 0 and WANT in got.ids
    assert all(src in got.ids for src in got.linked.values())
