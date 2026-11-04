"""Offline tests for private_context_needs on a tiny SYNTHETIC fixture built here (invented content, no real data)."""
import json

import common  # noqa: F401  (puts the components folder on sys.path)
from private_context_needs import dataset, score
from private_context_needs.impls import keyword_idf, qar_topic_cards, send_everything, send_nothing


def cand(i, typ, text, label):
    return dict(item_id=i, type=typ, text=text, label=label, why="invented")


def fixture():
    return [
        dict(case_id="c1", domain="planning_habits_goals", source_account_kind="team", self_contained=False,
             input_text="Summarise where the garden irrigation project stands and what was decided about the pump",
             candidates=[cand("a", "note", "Irrigation project: the pump was ordered and the trench for the pipe is half dug.", "needed"),
                         cand("b", "decision", "Decided to buy the larger irrigation pump so the pipe network can grow.", "needed"),
                         cand("c", "goal update", "Newsletter signups grew by ten percent after the spring campaign.", "irrelevant"),
                         cand("d", "note", "Office plants need watering on Mondays.", "irrelevant"),
                         cand("e", "card", "Gardening tools shed layout.", "helpful")]),
        dict(case_id="c2", domain="general_chat", source_account_kind="team", self_contained=True,
             input_text="Say hello and give me a two line poem about rain",
             candidates=[cand("f", "note", "Irrigation project: the pump was ordered.", "irrelevant"),
                         cand("g", "note", "Newsletter signups grew by ten percent.", "irrelevant")]),
    ]


def jsonl(cases):
    return "\n".join(json.dumps(c) for c in cases) + "\n"


def test_absent_dataset_loads_zero_cases_and_says_why(capsys):
    assert dataset.load_cases({}) == []
    assert "not set" in capsys.readouterr().out
    assert dataset.load_cases({dataset.ENV: "/nonexistent/path"}) == []
    assert "no cases.jsonl" in capsys.readouterr().out
    assert "no cases loaded" in score.aggregate([])


def test_loader_reads_a_folder_and_skips_malformed_lines(tmp_path):
    (tmp_path / "cases.jsonl").write_text(jsonl(fixture()) + "not json\n" + json.dumps(dict(case_id="x", candidates=[dict(label="bogus")])) + "\n")
    cases = dataset.load_cases({dataset.ENV: str(tmp_path)})
    assert [c["id"] for c in cases] == ["c1", "c2"] and cases[1]["self_contained"] and cases[0]["task"].startswith("Summarise")


def run_arm(fn):
    cases = dataset.parse_cases(jsonl(fixture()))
    return [score.score_case(fn, c) for c in cases]


def test_scorer_numbers_on_known_selections():
    c1 = fixture()[0]["candidates"]
    best = score.case_numbers(["a", "b"], c1, 0.0)
    assert best["recall"] == 1.0 and best["noise"] == 0.0 and best["ok"] and best["accuracy"] == 1.0
    noisy = score.case_numbers(["a", "c", "d"], c1, 0.0)
    assert noisy["recall"] == 0.5 and round(noisy["noise"], 4) == round(2 / 3, 4) and not noisy["ok"]
    nothing = score.case_numbers([], c1, 0.0)
    assert nothing["recall"] == 0.0 and nothing["accuracy"] == 0.0 and not nothing["ok"]
    alone = fixture()[1]["candidates"]
    assert score.case_numbers([], alone, 0.0)["accuracy"] == 1.0 and score.case_numbers([], alone, 0.0)["ok"]
    assert score.case_numbers(["f", "g"], alone, 0.0)["accuracy"] == 0.0
    assert score.case_numbers(["unknown-id"], c1, 0.0)["selected"] == 0   # an id outside the case is ignored


def test_balanced_score_ranks_arms_and_labels_never_reach_the_arm():
    seen = []

    def spy(task, candidates):
        seen.extend(sorted(k for c in candidates for k in c))
        return []
    run_arm(spy)
    assert set(seen) == {"item_id", "type", "text"}
    rows = {name: run_arm(mod.build(None)) for name, mod in
            (("nothing", send_nothing), ("everything", send_everything), ("idf", keyword_idf))}
    mean = {k: score.mean(v, "score") for k, v in rows.items()}
    assert mean["idf"] > mean["nothing"] and mean["idf"] > mean["everything"]
    assert "balanced score" in score.aggregate(rows["idf"])


def test_topic_card_arm_groups_sources_and_selects_cards():
    cases = dataset.parse_cases(jsonl(fixture()))
    groups, docs, idf = qar_topic_cards.group_topics([dict(item_id=c["item_id"], type=c["type"], text=c["text"]) for c in cases[0]["candidates"]])
    together = [g for g in groups if 0 in g]
    assert any(1 in g for g in together), "the note and the decision about the pump belong to one topic card"
    got = qar_topic_cards.build(None)(cases[0]["task"], [dict(item_id=c["item_id"], type=c["type"], text=c["text"]) for c in cases[0]["candidates"]])
    assert isinstance(got, list) and set(got) <= {"a", "b", "c", "d", "e"}
