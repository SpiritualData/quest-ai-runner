"""ARM ``place-on-a-quest:shared-quest-ranker``, second stage: the embedding order of the first stage, then the backend's prompt answered by a
model on the Claude Code subscription (``model_pick``). Counted model calls; always pass ``--max-calls`` and stay on the ladder of
esoi/metric-run-policy.md."""
import live_arm
from quest_ai_runner.api import JUDGE_WINDOW
from context_selection.embedder import embedder
from place_on_a_quest.backend_loader import backend
from place_on_a_quest.impls.shared_quest_ranker import candidates_by_embedding
from place_on_a_quest.model_pick import pick_among

PAID = True


def build(meter):
    pick, _ = backend()
    provider, model = live_arm.build(meter, "subscription")
    embed = embedder()

    def place(case):
        ordered = candidates_by_embedding(case, embed)
        return {"picked": pick_among(provider, model, pick, ordered, case, JUDGE_WINDOW), "ranked": [q["quest_id"] for q in ordered], "calls": 1}
    return place
