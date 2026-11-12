"""In affinity quest-scope mode the turn-start assembly hands every arm the turn's quest tags as soft ``affinity_tags``, not as a fence."""
from quest_ai_runner.the_intelligence.higher_mind.objectives import scope_tags as tags


def test_affinity_mode_moves_the_selected_quest_from_the_fence_to_the_rank(monkeypatch):
    meta = {"scope_tags": ["quest:q1"], "user_id": "u"}
    monkeypatch.setattr(tags, "quest_scope_mode", lambda: tags.QUEST_SCOPE_AFFINITY)
    moved = tags.affinity_meta(meta)
    assert moved["scope_tags"] == [] and moved["affinity_tags"] == ["quest:q1"] and moved["user_id"] == "u"
    monkeypatch.setattr(tags, "quest_scope_mode", lambda: tags.QUEST_SCOPE_FENCE)
    assert tags.affinity_meta(meta) is meta


def test_the_turn_start_call_applies_it():
    import inspect
    from quest_ai_runner.the_intelligence.mind.context_intake import orchestrator_run_begin as begin
    assert "affinity_meta(_ctx_meta)" in inspect.getsource(begin)
