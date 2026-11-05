"""metric-select-context builds on the hub's retrieval and bootstrap metrics and keeps no copy of their cases."""
import sys
import types

import common  # noqa: F401  (puts the components folder on sys.path)
from context_selection import foundation_adapter, private_cases
from esoi import access, config as esoi_config, foundation
from esoi import CallMeter

CONFIG = esoi_config.load(common.REPO)


def test_the_declaration_is_readable_and_leaves_the_private_source_a_share():
    assert foundation.problems(CONFIG) == []
    shares = {f.name: f.share for f in foundation.declared(CONFIG, "context_selection")}
    assert set(shares) == {"retrieval", "bootstrap"} and 0 < 1 - sum(shares.values()) < 1


def test_a_hub_arm_is_the_hub_arm_of_the_same_name_and_a_qar_arm_names_none():
    assert foundation.declared_arms(CONFIG, "context_selection", "hub_bm25_body") == {"retrieval": "bm25_body"}
    assert foundation.declared_arms(CONFIG, "context_selection", "vector_bge_small") == {}


def test_the_adapter_answers_the_hub_scorer_with_files_within_the_hubs_budget_and_only_for_retrieval(tmp_path, monkeypatch):
    texts = {"a.py": "x" * 200, "b.py": "y" * 400, "c.py": "z" * 40}
    for name, text in texts.items():
        (tmp_path / name).write_text(text)
    mix = types.ModuleType("context_retrieval.mix")
    mix.BUDGET_TOKENS = 100
    score = types.ModuleType("context_retrieval.score")
    score.world = lambda: dict(pin=types.SimpleNamespace(root=tmp_path, files=list(texts)), files=texts)
    package = types.ModuleType("context_retrieval")
    package.mix, package.score = mix, score
    monkeypatch.setitem(sys.modules, "context_retrieval", package)
    monkeypatch.setitem(sys.modules, "context_retrieval.mix", mix)
    monkeypatch.setitem(sys.modules, "context_retrieval.score", score)
    select = lambda task, corpus: ["b.py", "a.py", "missing.py", "c.py"]   # noqa: E731
    select.cards_per_file = 0
    assert foundation_adapter.foundation_fn("bootstrap", select) is None
    got = foundation_adapter.foundation_fn("retrieval", select)({"task": "t"})
    assert [(h["file"], h["tokens"]) for h in got["hits"]] == [("b.py", 100)]   # b alone fills the 100-token budget; nothing after it is shown
    assert got["index_bytes"] == 0 and got["units"] == 3 and got["build"] is not None


def test_a_private_case_reads_its_candidate_items_as_a_priced_source_read():
    meter = CallMeter()
    meter.access = {"source_access": {"usd_per_record": 0.5, "max_records_per_request": 3, "allowed_scopes": ["quest"]}}
    case = private_cases.parse_cases(
        '{"case_id": "r", "input_text": "t", "candidates": [{"item_id": "1", "label": "needed", "text": "abc"}, '
        '{"item_id": "2", "label": "irrelevant", "text": "de"}]}')[0]
    select = lambda task, corpus: ["item_000.txt"]   # noqa: E731
    select.meter = meter
    private_cases.score_case(select, case)
    assert (meter.reads.records, meter.reads.bytes, meter.reads.scopes) == (2, 5, {"quest"})
    assert meter.cost_usd >= 1.0 and access.violation(meter.reads, meter.access) == ""
