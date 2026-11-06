"""The private real inputs as a case source of the one context-selection metric, on a tiny SYNTHETIC dataset built here (invented content)."""
import hashlib
import os
import json
from pathlib import Path

import common  # noqa: F401  (puts the components folder on sys.path)
from context_selection import private_cases, score
from context_selection.impls import blind_grep, idf_docstrings
from esoi import CallMeter


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
    assert private_cases.load_cases({}) == []
    assert private_cases.digest_parts({}) == private_cases.ABSENT
    assert private_cases.digest_parts({private_cases.ENV: str(tmp_path / "nowhere")}) == private_cases.ABSENT
    assert "private 0 (dataset absent)" in score.aggregate([])


def test_the_digest_part_holds_the_hashes_of_both_private_files(tmp_path):
    env = write_dataset(tmp_path)
    cases_sha = hashlib.sha256((tmp_path / "cases.jsonl").read_bytes()).hexdigest()
    manifest_sha = hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    assert private_cases.digest_parts(env) == f"private:{cases_sha}/{manifest_sha}"
    (tmp_path / "cases.jsonl").write_text("\n", encoding="utf-8")
    assert private_cases.digest_parts(env) != f"private:{cases_sha}/{manifest_sha}"


def test_the_mix_takes_every_case_in_the_manifest_kind_proportions(tmp_path):
    env = write_dataset(tmp_path, tuple((f"k{i}", n) for i, n in enumerate([0] * 5 + [1] * 10 + [2] * 5)))
    cases = private_cases.mix(private_cases.parse_cases((tmp_path / "cases.jsonl").read_text()), private_cases.kind_shares(tmp_path), 10)
    assert len(cases) == 10 and [c["kind"] for c in cases].count("none") == 2 and [c["kind"] for c in cases].count("several") == 3
    assert private_cases.load_cases(env) and all(c["id"].startswith("PRIV-") for c in private_cases.load_cases(env))
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


def test_the_private_aggregate_names_the_count_and_the_accuracy_of_the_private_source():
    rows = [dict(ok=False, private=True, accuracy=0.2), dict(ok=True, private=True, accuracy=0.8)]
    assert score.aggregate(rows).startswith("case sources private 2 | real inputs 1/2 (50%), mean accuracy 0.500")


def test_a_partial_run_says_how_many_of_the_loaded_cases_it_scored(tmp_path, monkeypatch):
    write_dataset(tmp_path, (("a", 0), ("b", 1), ("c", 1)))
    monkeypatch.setenv(private_cases.ENV, str(tmp_path))
    assert "private 1 of 3 loaded" in score.aggregate([dict(ok=True, private=True, accuracy=1.0)])
    assert score.aggregate([dict(ok=True, private=True, accuracy=1.0)] * 3).startswith("case sources private 3 |")


def test_the_scorer_keeps_no_copy_of_the_hub_cases(tmp_path):
    assert score.load_cases() == private_cases.load_cases()
    assert not (Path(score.__file__).parent / "cases.json").exists() and not hasattr(score, "score_topic")


def test_several_folders_load_dedupe_and_digest_in_a_stable_order(tmp_path):
    one, two, gone = tmp_path / "one", tmp_path / "two", tmp_path / "gone"
    one.mkdir(), two.mkdir()
    write_dataset(one, (("a", 0), ("b", 1)))
    write_dataset(two, (("b", 2), ("c", 1)))
    env = {private_cases.ENV: os.pathsep.join([str(one), str(gone), str(two)])}
    flipped = {private_cases.ENV: os.pathsep.join([str(two), str(one)])}
    assert [f.name for f in private_cases.present_folders(env)] == ["one", "two"]
    loaded = private_cases.load_all(private_cases.present_folders(env))
    assert [c["id"] for c in loaded] == ["PRIV-a", "PRIV-b", "PRIV-c"]
    assert [c for c in loaded if c["id"] == "PRIV-b"][0]["kind"] == "single"      # the earlier folder wins the clash
    assert private_cases.digest_parts(env) == private_cases.digest_parts(flipped) and "+" in private_cases.digest_parts(env)
    assert private_cases.digest_parts({private_cases.ENV: str(gone)}) == private_cases.ABSENT


def test_kind_shares_sum_manifest_counts_else_use_the_cases_else_the_fallback(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    for folder, counts in ((one, dict(none=1, single=1, multi_same_type=0, multi_source=0)), (two, dict(none=1, single=0, multi_same_type=1, multi_source=1))):
        (folder / "manifest.json").write_text(json.dumps(dict(input_kind_mix=dict(counts=counts))))
    assert private_cases.kind_shares([one, two]) == dict(none=0.4, single=0.2, several=0.4)
    (two / "manifest.json").write_text("{}")
    cases = private_cases.parse_cases("".join(json.dumps(raw_case(n, k)) + "\n" for n, k in (("p", 0), ("q", 1), ("r", 1), ("s", 1))))
    assert private_cases.kind_shares([one, two], cases) == dict(none=0.25, single=0.75, several=0.0)
    assert private_cases.kind_shares([one, two]) == private_cases.KIND_MIX


def test_the_card_starts_with_item_metadata_and_a_path_item_gets_a_safe_unique_path():
    raw = raw_case("m", 0, extra=0, task="Do the thing")
    raw["request"] = dict(quest_title="Garden", goal_title="Water")
    raw["candidates"] = [
        dict(item_id="1", type="note", label="needed", text="pump order", describe="Quest note by Joshua via email, 3 days before"),
        dict(item_id="2", type="file", label="helpful", text="code", source_kind="repo_file", field="irrigation.py", author="Joshua",
             channel="file", age_label="2 days before", quest_relation="same_quest", status="active", path="/abs/../src/irrigation.py"),
        dict(item_id="3", type="file", label="helpful", text="more", path="src/irrigation.py"),
        dict(item_id="4", type="note", label="irrelevant", text="plain")]
    case = private_cases.parse_cases(json.dumps(raw))[0]
    assert case["task"] == "Quest: Garden. Goal: Water.\nDo the thing"
    corpus, owner = private_cases.item_corpus(case)
    cards = [corpus.cards[f"item-{i:03d}"]["summary"] for i in range(4)]
    assert cards[0].startswith("Quest note by Joshua via email, 3 days before: pump order")
    assert cards[1].startswith("repo_file: irrigation.py by Joshua via file, 2 days before, same_quest, active: code")
    assert cards[3] == "note: plain"
    assert sorted(owner) == ["abs/src/irrigation.py", "item_002.txt", "item_003.txt", "src/irrigation.py"] or "src/irrigation.py" in owner
    assert all(not rel.startswith("/") and ".." not in rel.split("/") for rel in owner) and len(owner) == 4
    assert (corpus.root / "src" / "irrigation.py").exists() or (corpus.root / "abs" / "src" / "irrigation.py").exists()


def test_a_case_without_titles_keeps_its_task_text():
    assert private_cases.parse_cases(json.dumps(raw_case("v", 1, task="Plain")))[0]["task"] == "Plain"
    only_goal = dict(raw_case("g", 1, task="T"), request=dict(goal_title="G"))
    assert private_cases.parse_cases(json.dumps(only_goal))[0]["task"] == "Goal: G.\nT"


def test_edit_recall_and_cross_quest_recall_are_reported_only_when_the_case_has_such_items():
    items = [dict(item_id="e", label="needed", need_role="edit_target", quest_relation="same_quest"),
             dict(item_id="f", label="needed", need_role="fact_source", quest_relation="other_team"),
             dict(item_id="g", label="needed", need_role="edit_target", quest_relation="no_quest"),
             dict(item_id="n", label="irrelevant")]
    row = private_cases.numbers(["e", "f", "n"], items)
    assert row["edit_recall"] == 0.5 and row["cross_quest_recall"] == 0.5
    fenced = private_cases.numbers(["e"], items)
    assert fenced["cross_quest_recall"] == 0.0 and fenced["accuracy"] == private_cases.numbers(["e"], [dict(i) for i in items])["accuracy"]
    plain = private_cases.numbers(["z-n0"], raw_case("z", 1)["candidates"])
    assert "edit_recall" not in plain and "cross_quest_recall" not in plain
    case = dict(id="PRIV-t", task="x", kind="single", candidates=items)
    scored = private_cases.score_case(lambda task, corpus: [r for r in corpus.files][:1], case)
    assert "edit recall" in scored["note"]
