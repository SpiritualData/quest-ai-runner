"""The private real inputs as a case source of the one context-selection metric, on a tiny SYNTHETIC dataset built here (invented content)."""
import hashlib
import json

import common  # noqa: F401  (puts the components folder on sys.path)
from context_selection import private_cases, score
from context_selection.impls import blind_grep, idf_docstrings
from esoi.harness import CallMeter


def cand(item_id, kind, text, label):
    return dict(item_id=item_id, type=kind, text=text, label=label, why="invented")


def raw_case(case_id, needed, extra=2, task="Where does the garden irrigation project stand"):
    items = [cand(f"{case_id}-n{i}", "note", f"Irrigation project note {i}: the pump was ordered and the trench is half dug.", "needed") for i in range(needed)]
    items += [cand(f"{case_id}-x{i}", "decision", f"Newsletter signups grew after the spring campaign {i}.", "irrelevant") for i in range(extra)]
    return dict(case_id=case_id, input_text=task, candidates=items)


def write_dataset(folder, counts=(("a", 0), ("b", 1), ("c", 1), ("d", 2))):
    text = "".join(json.dumps(raw_case(name, n)) + "\n" for name, n in counts)
    (folder / "cases.jsonl").write_text(text, encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(dict(input_kind_mix=dict(proportions=dict(none=0.25, single=0.5, multi_same_type=0.2, multi_source=0.05)))))
    return {private_cases.ENV: str(folder)}


def test_absent_dataset_contributes_nothing_and_records_an_absent_digest(tmp_path):
    assert private_cases.load_cases([{}] * 10, {}) == []
    assert private_cases.digest_parts({}) == private_cases.ABSENT
    assert private_cases.digest_parts({private_cases.ENV: str(tmp_path / "nowhere")}) == private_cases.ABSENT
    rows = [dict(ok=True, top1=True, top3=True, budget=3)]
    assert "private 0 (dataset absent)" in score.aggregate(rows)


def test_the_digest_part_holds_the_hashes_of_both_private_files(tmp_path):
    env = write_dataset(tmp_path)
    cases_sha = hashlib.sha256((tmp_path / "cases.jsonl").read_bytes()).hexdigest()
    manifest_sha = hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    assert private_cases.digest_parts(env) == f"private:{cases_sha}/{manifest_sha}"
    (tmp_path / "cases.jsonl").write_text("\n", encoding="utf-8")
    assert private_cases.digest_parts(env) != f"private:{cases_sha}/{manifest_sha}"


def test_the_mix_takes_the_weighted_count_in_the_manifest_kind_proportions(tmp_path):
    env = write_dataset(tmp_path, tuple((f"k{i}", n) for i, n in enumerate([0] * 5 + [1] * 10 + [2] * 5)))
    other = [{}] * 20          # equal weights: as many private cases as the two other sources together over two = 10
    cases = private_cases.mix(private_cases.parse_cases((tmp_path / "cases.jsonl").read_text()), private_cases.kind_shares(tmp_path), 10)
    assert len(cases) == 10 and [c["kind"] for c in cases].count("none") == 2 and [c["kind"] for c in cases].count("several") == 3
    assert private_cases.load_cases(other, env) and all(c["id"].startswith("PRIV-") for c in private_cases.load_cases(other, env))
    assert set(private_cases.kind_shares(tmp_path)) == {"none", "single", "several"}


def test_numbers_of_known_selections():
    case = raw_case("z", 2)["candidates"]
    best = private_cases.numbers(["z-n0", "z-n1"], case)
    assert best["accuracy"] == 1.0 and best["noise"] == 0.0
    half = private_cases.numbers(["z-n0", "z-x0"], case)
    assert half["recall"] == 0.5 and half["noise"] == 0.5 and half["accuracy"] == 0.5
    assert private_cases.numbers([], case)["accuracy"] == 0.0
    alone = raw_case("w", 0)["candidates"]
    assert private_cases.numbers([], alone)["accuracy"] == 1.0 and private_cases.numbers(["w-x0", "w-x1"], alone)["accuracy"] < 0.5
    assert private_cases.numbers(["unknown"], case)["selected"] == 0


def test_an_arm_scores_a_private_case_as_a_builder_over_the_items_and_pays_for_the_context(tmp_path):
    case = private_cases.parse_cases(json.dumps(raw_case("q", 1, extra=3)))[0]
    for arm in (blind_grep, idf_docstrings):
        meter = CallMeter()
        row = score.score_case(arm.build(meter), case)
        assert row["private"] and row["ok"] and row["recall"] == 1.0, arm.__name__
        assert meter.cost_usd > 0 or arm is blind_grep and row["size_chars"] > 0
    labels_seen = []
    corpus, owner = private_cases.item_corpus(case)
    labels_seen += [k for card in corpus.cards.values() for k in card]
    assert "label" not in labels_seen and len(owner) == 4


def test_the_private_aggregate_names_the_counts_of_each_source():
    rows = [dict(ok=True, top1=True, top3=True, budget=3), dict(ok=True, topic=True, cards_per_file=0.1, gated=False),
            dict(ok=False, private=True, accuracy=0.2)]
    assert score.aggregate(rows).startswith("case sources file 1, topic 1, private 1 |")
