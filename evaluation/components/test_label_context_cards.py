"""Offline tests for label_context_cards: every label names its source, the real generator, store and placement run in each arm, the
floor places by folder and stem and retrieves what its words reach, the deliberately broken baseline (no metadata) fails every retrieval
case, a scripted model that follows the labels passes, and the shipped placement call is pinned: a provider without ``generate`` places
nothing. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_label_context_cards.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from label_context_cards import arms, score  # noqa: E402

CASES = json.loads((HERE / "label_context_cards" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "label_context_cards")(common.CallMeter(max_calls=0))


class ScriptedProvider:
    """Answers the generator with the metadata a person would write and the placement with the labelled cards."""

    def __init__(self, metadata=None):
        self.metadata, self.answered = metadata or {}, 0

    def answer(self, messages, *, model=None, system=None):  # noqa: ARG002
        self.answered += 1
        return json.dumps(self.metadata)

    def generate(self, prompt, model=None, max_tokens=500, temperature=0.3):  # noqa: ARG002
        return "\n".join(f"{f} -> {c}" for f, c in BY_ID["LCC-PLC-01"]["label"].items())


def test_every_case_names_its_source_and_a_label():
    assert len(BY_ID) == len(CASES) >= 4 and {c["kind"] for c in CASES} == {"retrieve", "place"}
    assert all(len(c["source"]) > 60 for c in CASES)
    for c in CASES:
        if c["kind"] == "retrieve":
            assert any(q["expect"] is None for q in c["queries"]) and any(q["expect"] for q in c["queries"])
            assert {q["expect"] for q in c["queries"] if q["expect"]} <= {col["id"] for col in c["collections"]}
        else:
            assert set(c["label"]) == set(c["files"])


def test_no_case_carries_a_dash_an_address_or_a_real_path():
    blob = json.dumps(CASES, ensure_ascii=False)
    assert "—" not in blob and "–" not in blob and "/home/" not in blob and "@" not in blob


def test_a_card_with_the_right_metadata_is_retrieved_and_does_not_leak():
    case = BY_ID["LCC-RET-03"]
    meta = {"title": "Trip Plans", "description": "A five day trip itinerary with booking gaps.", "keywords": ["travel", "itinerary", "trip", "plan"]}
    got = arms.run_retrieve(case, ScriptedProvider(meta), "stub")
    assert got["hits"] == [True] and got["leaks"] == [False] and not got["fallback"]
    assert score.score_case(lambda c: got, case)["ok"]


def test_the_generators_fallback_is_flagged_and_a_card_without_metadata_is_invisible():
    case = BY_ID["LCC-RET-03"]
    got = arms.run_retrieve(case, ScriptedProvider(), "stub")   # a model that answers {} is not the fallback; an unparsable answer is
    class Garbled(ScriptedProvider):
        def answer(self, *a, **k):
            return "not json"
    broken = arms.run_retrieve(case, Garbled(), "stub")
    assert broken["fallback"] and broken["hits"] == [False]
    assert got["hits"] == [False]


def test_the_floor_places_by_folder_and_word_stem_and_asks_no_model():
    placed = arm("path_rules")(BY_ID["LCC-PLC-01"])["placed"]
    assert placed == BY_ID["LCC-PLC-01"]["label"]


def test_a_scripted_model_places_the_labelled_files_through_the_real_updater():
    got = arms.run_place(BY_ID["LCC-PLC-01"], ScriptedProvider(), "stub")
    assert score.score_case(lambda c: got, BY_ID["LCC-PLC-01"])["ok"]


def test_the_shipped_wiring_places_nothing_because_a_real_provider_has_no_generate():
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    from quest_ai_runner.adapters.gemini_provider import GeminiProvider
    from quest_ai_runner.adapters.multi_provider import MultiProvider
    assert not any(hasattr(p, "generate") for p in (ClaudeCliProvider, GeminiProvider, MultiProvider))
    class AnswerOnly:
        def answer(self, *a, **k):
            return "src/habits/handler.py -> habits-tracking"
    got = arms.run_place(BY_ID["LCC-PLC-01"], AnswerOnly(), "stub")
    assert set(got["placed"].values()) == {None}


def test_the_broken_baseline_fails_every_retrieval_case_and_misplaces_files():
    broken = common.load_impl("label_context_cards.broken.topic_for_all:build", "label_context_cards")(common.CallMeter(max_calls=0))
    rows = {c["id"]: score.score_case(broken, c) for c in CASES}
    assert not any(r["ok"] for r in rows.values()) and rows["LCC-PLC-01"]["right"] == 3


def test_the_scorer_reports_retrieval_and_placement_apart():
    line = score.aggregate([dict(ok=True, kind="retrieve", hits=2, hit_total=2, leaks=0, calls=1), dict(ok=False, kind="place", right=3, files=4, calls=1)])
    assert "hits 2/2" in line and "placed in the labelled card 3/4" in line
