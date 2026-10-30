"""The hybrid assembler lists the union of both arms best first (reciprocal-rank fusion), not keyword
cards then vector cards sorted by id."""
from quest_ai_runner.adapters.hybrid_context_assembler import fused_card_order
from hubofhubs.ports.context import AssembledContext


def keyword(*ids):
    return AssembledContext(context_view="k", card_ids=list(ids))


def vector(**scores):
    """Vector arm: card_ids sorted by id (as the live arm renders them), scores in the metadata."""
    ids = sorted(scores)
    return AssembledContext(context_view="v", card_ids=ids,
                            card_metadata=[{"id": i, "effective_score": scores[i]} for i in ids])


def test_card_both_arms_found_leads():
    assert fused_card_order(keyword("a", "b", "c"), vector(b=0.9, z=0.5, y=0.1))[0] == "b"


def test_vector_order_follows_score_not_id():
    order = fused_card_order(keyword(), vector(a=0.1, b=0.9, c=0.5))
    assert order == ["b", "c", "a"]


def test_keyword_order_is_kept_for_single_arm_cards():
    assert fused_card_order(keyword("x", "y", "z"), vector()) == ["x", "y", "z"]


def test_priority_cards_lead_and_union_is_deduped():
    order = fused_card_order(keyword("a", "b"), vector(b=0.9, c=0.8), priority=["c"])
    assert order[0] == "c" and sorted(order) == ["a", "b", "c"]
