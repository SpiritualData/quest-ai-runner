"""ARM ``place-on-a-quest:hybrid-candidate-ranker`` (quest-backend ``quest_candidates.rank_quests_for_message``): IDF-weighted word overlap over
each quest's whole document plus an embedding bonus, with evidence snippets. The placement is the leader ``best_ranked_quest_candidate`` accepts
(a score floor and a margin over the runner-up), none when there is no leader. The shipped functions run as they are (placement asks for ``identify_terms=False``, as ``quest_selection.candidate_leaders`` does: the model's
reading of which words name a quest only decides which terms bind a WRITE, never which quest leads); only the two database reads are replaced by
the case's fixtures: the quest index and the embedding search (local bge-small cosine scores for the quest cards, under a floor of
``SEMANTIC_FLOOR``). Zero model calls."""
import asyncio

import numpy as np

from context_selection.embedder import embedder
from place_on_a_quest import account
from place_on_a_quest.backend_loader import backend, quest_index

ALTERNATIVE = True   # measures the quest-backend code, which esoi cannot see from this repository
PAID = False
SEMANTIC_FLOOR = 0.6   # the backend card floor (0.45) sits just above unrelated text on its production embeddings; bge-small scores unrelated text 0.48 to 0.63, so the floor is scaled up to the same place


def build(meter):  # noqa: ARG001
    _, candidates = backend()
    embed = embedder()

    def place(case):
        message = case["message"]
        quests = account.quests_of(case)
        cards = embed([account.card_text(q) for q in quests])
        cards = cards / np.linalg.norm(cards, axis=1, keepdims=True)
        index = quest_index(candidates, quests)

        async def build_index(user_id, reach=None):  # noqa: ARG001
            return index

        async def semantic(user_id, text, limit=5):  # noqa: ARG001
            query = embed([text])[0]
            sims = cards @ (query / np.linalg.norm(query))
            order = np.argsort(-sims, kind="stable")[:limit]
            return [(quests[i]["quest_id"], float(sims[i])) for i in order if sims[i] >= SEMANTIC_FLOOR]

        candidates.build_user_quest_index, candidates.semantic_quest_candidates = build_index, semantic
        ranked = asyncio.run(candidates.rank_quests_for_message("user", message, top_k=len(quests), identify_terms=False))
        leader, has_leader = candidates.best_ranked_quest_candidate(ranked)
        rest = [q["quest_id"] for q in ranked]
        return {"picked": [leader] if has_leader and leader else [], "ranked": rest, "calls": 0}
    return place
