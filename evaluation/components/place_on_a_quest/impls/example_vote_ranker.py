"""ARM ``place-on-a-quest:example-vote-ranker`` (quest-backend ``quest_selection.select_and_rank_quests_for_text`` with ``quest_examples``): the
hybrid ranking of the first arm, fused with the similarity-weighted vote of the earlier items the person already placed on a quest.

A case may carry ``examples``: the person's earlier items, each ``{"text", "quest_id"}`` (optionally ``"weight"``, the provenance weight, and
``"moved"``). The ``k`` most similar to the message by the local embedder (the same stand-in every arm uses) are the examples; their vote
(the backend's own ``example_vote``) and the hybrid order are fused (``fuse_orders``), and the pick is the vote's leader when there are examples
(the answer the backend gives when the judge picks none), else the hybrid arm's pick. A case with no examples therefore places exactly as
the hybrid arm does. Zero model calls."""
import numpy as np

from context_selection.embedder import embedder
from place_on_a_quest import account
from place_on_a_quest.backend_loader import examples as backend_examples
from place_on_a_quest.impls import hybrid_candidate_ranker

ALTERNATIVE = True   # measures the quest-backend code, which esoi cannot see from this repository
PAID = False


def similar_examples(case, embed):
    """The case's examples closest to its message, best first, as the backend's ``similar_examples`` returns them (repeats kept once)."""
    found = case.get("examples") or []
    if not found:
        return []
    E = backend_examples()
    docs = embed([e["text"] for e in found])
    docs = docs / np.linalg.norm(docs, axis=1, keepdims=True)
    query = embed([case["message"]])[0]
    sims = docs @ (query / np.linalg.norm(query))
    scored = [{"quest_id": found[i]["quest_id"], "text": found[i]["text"], "weight": found[i].get("weight", 1.0),
               "moved": bool(found[i].get("moved")), "score": float(sims[i])} for i in np.argsort(-sims, kind="stable")]
    return E.distinct_examples(scored[:E.EXAMPLES_RETRIEVED * 3])[:E.EXAMPLES_RETRIEVED]


def fused_order(case, hybrid_ranked, examples):
    """The quest ids for the case: the vote and the hybrid order fused, then every quest the case has."""
    E = backend_examples()
    vote = E.example_vote(examples)
    order = E.fuse_orders(E.vote_order(vote), list(hybrid_ranked)) if vote else list(hybrid_ranked)
    return order + [q["quest_id"] for q in account.quests_of(case) if q["quest_id"] not in order], vote


def build(meter):
    rank = hybrid_candidate_ranker.build(meter)
    embed = embedder()

    def place(case):
        first = rank(case)
        examples = similar_examples(case, embed)
        order, vote = fused_order(case, first["ranked"], examples)
        picked = [backend_examples().vote_order(vote)[0]] if vote else first["picked"]
        return {"picked": picked, "ranked": order, "calls": 0}
    return place
