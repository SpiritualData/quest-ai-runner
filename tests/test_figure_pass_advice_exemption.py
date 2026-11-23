"""General advice that read none of the person's records keeps its figures; a reply that had records stays checked."""
from types import SimpleNamespace

from hubofhubs.api import strip_unsupported

from quest_ai_runner.intelligence.mind.orchestrator_run_loop import figures_exempt
from quest_ai_runner.intelligence.mind.response_writer.reply_standards import SHAPE_ADVICE


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


def test_a_general_knowledge_reach_verdict_keeps_a_conversion_figure_but_records_still_check():
    assert figures_exempt(turn(shape="default"), {"reach": "general"}) is True
    assert figures_exempt(turn(shape="default"), {"reach": "own_records"}) is False
    assert figures_exempt(turn(shape="default", gathered=[{"text": "Run log: 3 sessions"}]), {"reach": "general"}) is False
    # a quest's records sitting in the context do not make a general question's figures unsafe
    assert figures_exempt(turn(shape="default", sources=[{"adapter": "records", "items": ["quest-note:q1:0"]}]), {"reach": "general"}) is True
    assert figures_exempt(turn(shape="default", sources=[{"adapter": "records", "items": ["quest-note:q1:0"]}]), {"reach": "own_records"}) is False


def test_a_general_knowledge_reply_is_marked_once_and_a_records_turn_is_not():
    from quest_ai_runner.intelligence.mind.orchestrator_run_loop import mark_general_knowledge
    rs = turn(shape="default")
    assert mark_general_knowledge("375 F is about 190 C.", rs, {"reach": "general"}).endswith("(General knowledge, not from your records.)")
    assert mark_general_knowledge("About 190 C, general knowledge.", rs, {"reach": "general"}) == "About 190 C, general knowledge."
    assert mark_general_knowledge("190 C", rs, {"reach": "own_records"}) == "190 C"
    assert mark_general_knowledge("190 C", turn(gathered=[{"text": "x", "locator": "file:a"}]), {"reach": "general"}) == "190 C"
    assert mark_general_knowledge("Sage freundlich ab und schlage einen neuen Termin vor, das ist hoeflich.", rs, {"reach": "general"}).endswith("hoeflich.")
