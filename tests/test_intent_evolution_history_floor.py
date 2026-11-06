"""The no-model floor arm of Intent Evolution: restraint, what worked, or one experiment."""
from quest_ai_runner.the_consciousness.intent_evolution.history_floor import HistoryFloor
from quest_ai_runner.the_consciousness.intent_evolution.interface import IntentEvolving


def test_arm_is_an_intent_evolving():
    assert isinstance(HistoryFloor(), IntentEvolving)


def test_no_intents_or_a_running_experiment_holds_back():
    assert HistoryFloor().suggest({"intents": []})["kind"] == "none"
    running = {"intents": ["Ship the note"], "experiments": [{"status": "running"}]}
    assert HistoryFloor().suggest(running)["kind"] == "none"


def test_what_worked_before_is_offered_again_with_its_metric():
    quest = {"intents": ["Ship the note"], "metrics": [{"name": "notes shipped"}],
             "history": [{"text": "Write for 20 minutes", "worked": True, "metric": "minutes written"}, {"text": "Skip", "worked": False}]}
    assert HistoryFloor().suggest(quest) == {"kind": "option", "text": "Write for 20 minutes", "metric": "minutes written"}


def test_otherwise_one_experiment_checked_by_the_first_metric():
    out = HistoryFloor().suggest({"intents": ["Ship the note"], "metrics": ["notes shipped"], "experiments": [{"status": "done"}]})
    assert out["kind"] == "experiment" and out["metric"] == "notes shipped" and "Ship the note" in out["text"]
