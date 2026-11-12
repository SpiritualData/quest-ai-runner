"""General advice that read none of the person's records keeps its figures; a reply that had records stays checked."""
from types import SimpleNamespace

from hubofhubs.api import strip_unsupported

from quest_ai_runner.the_intelligence.mind.orchestrator_run_loop import figures_exempt
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import SHAPE_ADVICE


def turn(sources=None, gathered=None, shape=SHAPE_ADVICE):
    plan = SimpleNamespace(reply_shape=shape, user_intent="ask", delivery=None, action="answer", asks_first=False, deep_target=None, deferred_deep=None)
    return SimpleNamespace(plan=plan, turn_user_intent="ask", turn_delivery=None, _sources=sources or [], gathered=gathered or [])


def test_advice_without_records_is_exempt_and_with_records_is_not():
    assert figures_exempt(turn()) is True
    assert figures_exempt(turn(sources=[{"adapter": "records", "items": ["quest-note:q1:0"]}])) is False
    assert figures_exempt(turn(gathered=[{"text": "Run log: 3 sessions"}])) is False
    assert figures_exempt(turn(shape="default")) is False


def test_the_figure_cut_still_removes_an_unsupported_claim_about_the_person():
    text = "Try 10 minutes a day. You logged 12 sessions last month."
    cut, dropped = strip_unsupported(text, "walks of 10 minutes; run log: 3 sessions")
    assert dropped == ["12"] and "12 sessions" not in cut and "10 minutes" in cut
