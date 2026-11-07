"""``place-on-a-quest``: the port the host fills and the library arm for a runner with no host."""
from quest_ai_runner.the_intelligence.higher_mind.objectives.interface import QuestPlacing
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_placement import HostFunction, LibraryQuestJudge

QUESTS = [{"quest_id": "a", "title": "Ship the app", "state": ""}, {"quest_id": "b", "title": "Run a marathon", "state": ""}]


def test_host_function_returns_the_hosts_picks_and_ranking():
    host = HostFunction(lambda **ask: {"picked": ["b"], "ranked": ["b", "a"], "seen": ask})
    placed = host.place("long run today", "", "a", 1)
    assert placed == {"picked": ["b"], "ranked": ["b", "a"]} and isinstance(host, QuestPlacing)


def test_host_function_passes_the_whole_ask_to_the_hosts_function():
    seen = {}
    HostFunction(lambda **ask: seen.update(ask) or {"picked": [], "ranked": []}).place("hi", "before", "a", 3)
    assert seen == {"message": "hi", "previous": "before", "home_quest_id": "a", "max_quests": 3}


def test_host_function_failure_is_no_placement_not_a_guess():
    def down(**ask):
        raise RuntimeError("host down")
    assert HostFunction(down).place("anything") == {"picked": [], "ranked": []}


def test_empty_picks_mean_no_quest_fits():
    assert HostFunction(lambda **ask: {"picked": [], "ranked": ["a", "b"]}).place("weather")["picked"] == []


def test_library_judge_places_on_the_quest_the_model_names_and_none_otherwise():
    def model(prompt, schema):
        return {"results": [{"item": 1, "quest_id": "b" if "run" in prompt else "", "ranking": ["b", "a"]}]}
    judge = LibraryQuestJudge(QUESTS, model)
    assert judge.place("go for a run")["picked"] == ["b"] and isinstance(judge, QuestPlacing)
    assert judge.place("what is the weather")["picked"] == []
