"""ARM ``place-on-a-quest:hybrid-candidate-ranker``, second stage: the candidates in the order the hybrid ranking gives them (word overlap plus
embedding bonus; quests it did not score follow in the account's order), then the backend's prompt answered by a model on the Claude Code
subscription (``model_pick``). The hybrid ranking has no model pick of its own (its model step only marks which words name a quest, for the write
bind), so this is what the ranking is for: candidates a model chooses among. Counted model calls; always pass ``--max-calls``."""
import live_arm
from quest_ai_runner.api import JUDGE_WINDOW
from place_on_a_quest import account
from place_on_a_quest.backend_loader import backend
from place_on_a_quest.impls import hybrid_candidate_ranker
from place_on_a_quest.model_pick import pick_among

PAID = True


def build(meter):
    pick, _ = backend()
    provider, model = live_arm.build(meter, "subscription")
    rank = hybrid_candidate_ranker.build(meter)

    def place(case):
        first = rank(case)["ranked"]
        quests = account.quests_of(case)
        by_id = {q["quest_id"]: q for q in quests}
        ordered = [by_id[i] for i in first] + [q for q in quests if q["quest_id"] not in first]
        return {"picked": pick_among(provider, model, pick, ordered, case, JUDGE_WINDOW), "ranked": [q["quest_id"] for q in ordered], "calls": 1}
    return place
