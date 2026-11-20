"""The planner's web block sends a current-price question about one named product to the seller's own pricing page."""
from quest_ai_runner.intelligence.mind.context_intake import planner_context


def test_seller_price_rule_present():
    text = " ".join(str(v) for v in vars(planner_context).values() if isinstance(v, str))
    assert "seller's own pricing page" in text
    assert "third-party sites state it" in text
