"""quest_scope_mode: `fence` (default) hides another quest's items; `affinity` keeps them, ranks them lower and labels them.

Synthetic data only, fully offline."""
import pytest

from quest_ai_runner.core import scope_tags as st
from quest_ai_runner.core.recent_context import FileRecentContextStore
from quest_ai_runner.core import runtime_settings
from quest_ai_runner.core.runtime_settings import CoreSettings, install_core_settings
from quest_ai_runner.config_core_settings import core_settings_from_env


@pytest.fixture
def mode():
    saved = runtime_settings._settings_source
    def set_mode(value):
        install_core_settings(CoreSettings(quest_scope_mode=value))
    yield set_mode
    install_core_settings(saved)


A, B = {"id": "a", "scope_tags": ["quest:a"]}, {"id": "b", "scope_tags": ["quest:b"]}


def test_default_is_fence_and_env_selects_affinity():
    assert CoreSettings().quest_scope_mode == "fence"
    assert core_settings_from_env({}).quest_scope_mode == "fence"
    assert core_settings_from_env({"QAR_QUEST_SCOPE_MODE": "Affinity"}).quest_scope_mode == "affinity"
    assert core_settings_from_env({"QAR_QUEST_SCOPE_MODE": "bogus"}).quest_scope_mode == "fence"


def test_fence_mode_is_todays_behaviour(mode):
    mode("fence")
    assert st.card_visible_to_turn(A, ["quest:a"]) and not st.card_visible_to_turn(B, ["quest:a"])
    assert st.fence_tags(["quest:a"]) == ["quest:a"]
    assert st.card_affinity_factor(B, ["quest:a"]) == 1.0 and st.other_quest_label(B, ["quest:a"]) == ""
    meta = {"scope_tags": ["quest:a"]}
    assert st.affinity_meta(meta) is meta
    assert "private to it" in st.scope_fence_block(["a"])


def test_affinity_mode_keeps_other_quests_ranks_and_labels_them(mode):
    mode("affinity")
    assert st.card_visible_to_turn(B, ["quest:a"])
    assert st.fence_tags(["quest:a", "team:t"]) == []           # no audience kinds exist today: the fence is empty
    assert st.card_affinity_factor(A, ["quest:a"]) == st.AFFINITY_HOME_BOOST
    assert st.card_affinity_factor(B, ["quest:a"]) == st.AFFINITY_AWAY_FACTOR
    assert st.card_affinity_factor({"id": "x"}, ["quest:a"]) == 1.0 and st.card_affinity_factor(B, None) == 1.0
    assert st.other_quest_label(B, ["quest:a"]) == "quest:b"
    assert st.other_quest_label({**B, "quest_title": "Running"}, ["quest:a"]) == "Running"
    assert st.other_quest_label(A, ["quest:a"]) == "" and st.other_quest_label(B, None) == ""
    assert st.affinity_meta({"scope_tags": ["quest:a"], "x": 1}) == {"scope_tags": [], "affinity_tags": ["quest:a"], "x": 1}
    assert "from another quest" in st.scope_fence_block(["a"])


def test_recent_context_hides_other_quests_only_in_fence_mode(mode, tmp_path):
    store = FileRecentContextStore(tmp_path)
    store.record(["quest:b", "global"], [{"id": "card-b", "files": []}], "learned on b")
    for value, expected in (("fence", []), ("affinity", ["card-b"])):
        mode(value)
        assert [r["id"] for r in store.load(["quest:a", "global"])] == expected


def test_keyword_store_labels_and_ranks_an_other_quest_card(mode):
    from quest_ai_runner.adapters.context_store_assembly import other_quest_label  # the label the render path uses
    mode("affinity")
    assert other_quest_label(B, ["quest:a"])
