"""The topic cases and the store charge of the context-selection score, offline."""
import common  # noqa: F401  (puts the components folder on sys.path)
from context_selection import score, topic_cases
from context_selection.corpus import build_corpus
from context_selection.impls import hub_folder_cards, hub_per_file_symbols
from context_selection.store_cost import cost_budget, store_charged
from esoi.harness import CallMeter


def test_the_topic_cases_are_pinned_and_name_no_file():
    cases = topic_cases.load()
    assert len(cases) >= 30 and all(len(c["needed"]) >= 2 for c in cases)
    _history, names_something, _commit = topic_cases.hub_helpers()
    assert not [c["task"] for c in cases if names_something(c["task"])]


def test_both_case_sources_are_cases_of_the_one_metric():
    cases = score.load_cases()
    assert {"target" in c for c in cases} == {True, False}
    assert len({c["id"] for c in cases}) == len(cases)


def test_a_card_per_file_store_passes_no_topic_case(tmp_path):
    (tmp_path / "pkg").mkdir()
    for name in ("a", "b", "c", "d"):
        (tmp_path / "pkg" / f"{name}.py").write_text(f'"""{name} notes."""\n', encoding="utf-8")
    corpus = build_corpus(tmp_path)
    needed = [f for f in corpus.files][:2]
    case = {"id": "T", "task": "x", "needed": needed}
    per_file = lambda task, corp: list(corp.files)                      # noqa: E731
    row = score.score_topic(per_file, case, corpus, per_file("x", corpus))
    assert row["gated"] and not row["ok"] and row["share"] == 1.0
    per_file.cards_per_file = 0.1
    assert score.score_topic(per_file, case, corpus, per_file("x", corpus))["ok"]


def test_the_store_charge_grows_with_the_share_of_the_card_limit(tmp_path):
    (tmp_path / "a.py").write_text('"""a."""\n', encoding="utf-8")
    corpus = build_corpus(tmp_path)
    meter = CallMeter()
    select = store_charged(lambda task, corp: [], meter, cards=lambda corp: 500)
    select("t", corpus)
    assert abs(meter.cost_usd - cost_budget() * 0.5) < 1e-9
    assert select.cards_per_file == 500


def test_the_hub_arms_build_cards_through_the_hub_helpers(tmp_path):
    (tmp_path / "pkg").mkdir()
    for name, word in (("a", "zebra"), ("b", "walrus")):
        (tmp_path / "pkg" / f"{name}.py").write_text(f'"""Notes on the {word}."""\n\ndef {word}_thing():\n    pass\n', encoding="utf-8")
    corpus = build_corpus(tmp_path)
    assert len(hub_folder_cards.folder_cards(corpus.root, corpus.files)) == 1
    select = hub_per_file_symbols.build(CallMeter())
    assert select("zebra", corpus)[0] == "pkg/a.py"
