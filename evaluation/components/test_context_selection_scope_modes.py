"""The private-case selector scored three ways (none, fence, affinity) on SYNTHETIC items (invented content)."""
import common  # noqa: F401  (puts the components folder on sys.path)
from context_selection import private_cases as pc


def item(item_id, label, relation=None, title=None, text="running schedule note"):
    return dict(item_id=item_id, type="note", text=text, label=label, quest_relation=relation, quest_title=title)


CASE = dict(id="PRIV-s", task="family schedule", kind="single", candidates=[
    item("home", "irrelevant", "same_quest"), item("away", "needed", "other_quest", "Running plan"),
    item("bare", "helpful"), item("anon", "irrelevant", "other_quest")])


def test_item_tags_follow_the_relation_then_the_title_else_other():
    assert pc.item_affinity_tags(CASE["candidates"][0]) == [pc.TURN_TAG]
    assert pc.item_affinity_tags(CASE["candidates"][1]) == ["quest:running-plan"]
    assert pc.item_affinity_tags(CASE["candidates"][2]) == []
    assert pc.item_affinity_tags(CASE["candidates"][3]) == ["quest:other"]


def test_scope_mode_flag_defaults_to_none_and_an_arm_may_override():
    assert pc.scope_mode_of(None, {}) == "none" and pc.scope_mode_of(None, {pc.SCOPE_ENV: "Affinity"}) == "affinity"
    assert pc.scope_mode_of(None, {pc.SCOPE_ENV: "bogus"}) == "none"
    assert pc.scope_mode_of(type("A", (), {"scope_mode": "fence"})(), {pc.SCOPE_ENV: "affinity"}) == "fence"


def test_none_keeps_every_item_untouched_fence_removes_other_quests_affinity_keeps_all():
    none_corpus, none_owner = pc.item_corpus(CASE)
    assert len(none_owner) == 4 and all("affinity_tags" not in c for c in none_corpus.cards.values())
    _, fence_owner = pc.item_corpus(CASE, "fence")
    assert sorted(fence_owner.values()) == ["bare", "home"]
    corpus, owner = pc.item_corpus(CASE, "affinity")
    assert len(owner) == 4 and corpus.cards["item-000"]["affinity_tags"] == [pc.TURN_TAG]


def test_affinity_reorders_toward_the_request_quest_without_dropping_anything():
    corpus, owner = pc.item_corpus(CASE, "affinity")
    by_item = {v: k for k, v in owner.items()}
    ranked = [by_item["away"], by_item["home"], by_item["bare"]]
    out = pc.reorder_by_affinity(ranked, owner, CASE["candidates"])
    assert [owner[r] for r in out] == ["home", "away", "bare"]
    assert sorted(out) == sorted(ranked)


def test_score_case_per_mode_fence_loses_the_cross_quest_item_affinity_finds_it():
    def arm(task, corpus):
        return sorted(corpus.files)
    results = {}
    for mode in pc.SCOPE_MODES:
        arm.scope_mode = mode
        results[mode] = pc.score_case(arm, CASE)
    assert results["fence"]["cross_quest_recall"] == 0.0
    assert results["affinity"]["cross_quest_recall"] == 1.0 == results["none"]["cross_quest_recall"]
