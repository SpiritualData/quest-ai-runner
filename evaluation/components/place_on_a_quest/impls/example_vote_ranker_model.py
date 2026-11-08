"""ARM ``place-on-a-quest:example-vote-ranker``, second stage: the candidates in the fused order of the first stage (hybrid ranking and example
vote), then the backend's prompt, with the earlier items shown after the quests, answered by a model on the Claude Code subscription
(``model_pick``). The pick is the model's alone (an empty answer is none). A case with no examples is the hybrid arm's second stage. Counted
model calls; always pass ``--max-calls``."""
import live_arm
from context_selection.embedder import embedder
from place_on_a_quest import account
from place_on_a_quest.backend_loader import backend
from place_on_a_quest.impls import example_vote_ranker
from place_on_a_quest.model_pick import JUDGE_CAP, pick_among

ALTERNATIVE = True   # measures the quest-backend code, which esoi cannot see from this repository
PAID = True


def build(meter):
    pick, _ = backend()
    provider, model = live_arm.build(meter, "subscription")
    rank = example_vote_ranker.hybrid_candidate_ranker.build(meter)
    embed = embedder()

    def place(case):
        first = rank(case)["ranked"]
        examples = example_vote_ranker.similar_examples(case, embed)
        order, _vote = example_vote_ranker.fused_order(case, first, examples)
        by_id = {q["quest_id"]: q for q in account.quests_of(case)}
        ordered = [by_id[i] for i in order]
        return {"picked": pick_among(provider, model, pick, ordered, {**case, "examples": examples}, JUDGE_CAP),
                "ranked": [q["quest_id"] for q in ordered], "calls": 1}
    return place
