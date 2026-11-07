"""ARM ``place-on-a-quest:shared-quest-ranker`` (quest-backend ``quest_selection.select_and_rank_quests_for_text``): the quests' own embeddings
(outcome, category and scope) order the candidates, then the model picks which numbered quests the message is about, or none.

Stage one (no model): the embedding order alone; the pick is the closest quest, which is what the backend falls back to when the model picks none
or fails. A message no quest fits therefore always gets a pick here. Stage two: the backend's own prompt (``quest_pick_prompt``) answered by the
Claude Code subscription's cheapest tier; the pick is the model's alone (an empty answer is none). The embedder is the local bge-small model that
stands in for the production embedding service, the same for every arm."""
import numpy as np

from context_selection.embedder import embedder
from place_on_a_quest import account

ALTERNATIVE = True   # measures the quest-backend code, which esoi cannot see from this repository
PAID = False


def candidates_by_embedding(case, embed):
    quests = account.quests_of(case)
    docs = embed([account.embedding_text(q) for q in quests])
    text = (case["previous"] + " " + case["message"]).strip() if len(case["message"].split()) < 6 and case["previous"] else case["message"]
    query = embed([text])[0]
    sims = (docs / np.linalg.norm(docs, axis=1, keepdims=True)) @ (query / np.linalg.norm(query))
    return [quests[i] for i in np.argsort(-sims, kind="stable")]


def build(meter):  # noqa: ARG001
    embed = embedder()

    def place(case):
        ranked = candidates_by_embedding(case, embed)
        return {"picked": [ranked[0]["quest_id"]], "ranked": [q["quest_id"] for q in ranked], "calls": 0}
    return place
