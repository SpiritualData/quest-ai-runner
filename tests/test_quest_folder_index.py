"""Each chat turn is grounded in the synced quest it is about, with no search and no model call.

Regression for 2026-09-26: "What full filepath is for the 1000 subscribers quest and what is the
next thing to do" went looking for the quest (and the narration guessed a path that did not exist)
while the folder's QUEST_SYNC.md held the id, state and next steps all along. Also covers the
opt-out: someone whose chats are mostly not about a quest can turn matching off, and that choice
is remembered.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile

import pytest

from quest_ai_runner.runner.quest_folder_index import (
    discover_quest_folders, match_quest_folder, render_quest_folder_context,
)


def make_quest(root, folder, quest_id, goal, state="", next_steps=""):
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    text = (f"---\nquest_id: {quest_id}\n---\n\n<!-- QAR:MANAGED:goal START -->\n"
            f"**Goal:** {goal}\n**Status:** In progress\n\n**Current state:**\n{state}\n"
            "<!-- QAR:MANAGED:goal END -->\n")
    if next_steps:
        text += (f"\n<!-- QAR:MANAGED:next_steps START -->\n## Next steps\n\n{next_steps}\n"
                 "<!-- QAR:MANAGED:next_steps END -->\n")
    (path / "QUEST_SYNC.md").write_text(text)
    (path / "PLAN.md").write_text("plan")
    return path


@pytest.fixture
def corpus(tmp_path):
    make_quest(tmp_path, "quest_subscribers_growth", "quest_subs", "Reach 1000 paying Quest subscribers",
               state="Pricing page has no direct checkout deep link. Warm leads outreach half done. "
                     "Stripe checkout wired.",
               next_steps="1. Ship the checkout deep link (target 2026-09-30)")
    make_quest(tmp_path, "quest_app_downloads", "quest_apps",
               "Quest mobile app gets 1000+ downloads on Google Play and the Apple store",
               state="Store listing screenshots are stale.")
    make_quest(tmp_path, "wikipedia_revolution", "quest_wiki",
               "Wikipedia parapsychology representation is approved by editors")
    make_quest(tmp_path / "deep" / "er", "nested_quest", "quest_nested", "Concept AI interface for researchers")
    make_quest(tmp_path, ".hidden_copy", "quest_hidden", "Hidden duplicate")
    return tmp_path


def test_discovery_finds_synced_folders_and_skips_hidden(corpus):
    found = {q.quest_id: q for q in discover_quest_folders(str(corpus))}
    assert set(found) == {"quest_subs", "quest_apps", "quest_wiki", "quest_nested"}
    assert found["quest_subs"].folder == str((corpus / "quest_subscribers_growth").resolve())
    assert found["quest_subs"].next_steps.startswith("## Next steps")


def test_mapped_folder_wins_over_a_copy(corpus, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("mapped")
    make_quest(elsewhere, "subs_real", "quest_subs", "Reach 1000 paying Quest subscribers")
    found = {q.quest_id: q for q in discover_quest_folders(
        str(corpus), {"quest_subs": str(elsewhere / "subs_real")})}
    assert found["quest_subs"].folder == str((elsewhere / "subs_real").resolve())


@pytest.mark.parametrize("message,expected", [
    ("What full filepath is for the 1000 subscribers quest and what is the next thing to do", "quest_subs"),
    ("how many paying subscribers do we have now", "quest_subs"),
    ("what's next for app downloads", "quest_apps"),
    ("status of the wikipedia work", "quest_wiki"),
    # Never named, but it is this quest's current state, word for word.
    ("did we fix the pricing page deep link", "quest_subs"),
    ("how is the warm leads outreach going", "quest_subs"),
    ("add a note to quest_apps please", "quest_apps"),
    # Nothing to do with any quest: no block.
    ("hello", None),
    ("what should I work on today", None),
    ("fix the logout bug in the frontend", None),
    # Shared number only ("1000" is in two quests): too weak to pick one.
    ("1000", None),
])
def test_matching(corpus, message, expected):
    found = match_quest_folder(message, discover_quest_folders(str(corpus)))
    assert (found.quest_id if found else None) == expected


def test_quest_card_carries_full_paths_state_and_next_steps(corpus):
    from quest_ai_runner.runner.quest_folder_index import quest_card
    quest = match_quest_folder("1000 subscribers", discover_quest_folders(str(corpus)))
    card = quest_card(quest, str(corpus))
    note = card["content"][0]["locator"]["text"]
    assert str((corpus / "quest_subscribers_growth").resolve()) in note
    assert "QUEST_SYNC.md" in note and "PLAN.md" in note
    assert "Ship the checkout deep link" in note
    assert card["files"][0]["path"] == "quest_subscribers_growth/QUEST_SYNC.md"
    assert card["scope_tags"] and card["managed_by"]
    assert render_quest_folder_context(None) == ""


def make_store(corpus):
    from quest_ai_runner.adapters.file_context_store import FileContextStore
    return FileContextStore(str(corpus / ".cards"), repo_root=str(corpus), auto_bootstrap=False)


def test_quest_cards_are_synced_once_and_refreshed_on_change(corpus):
    from quest_ai_runner.runner.quest_folder_index import quest_card_id, sync_quest_cards
    store = make_store(corpus)
    quests = discover_quest_folders(str(corpus))
    assert sync_quest_cards(store, quests, str(corpus)) == 4
    assert sync_quest_cards(store, quests, str(corpus)) == 0  # unchanged: nothing rewritten
    make_quest(corpus, "quest_subscribers_growth", "quest_subs", "Reach 1000 paying Quest subscribers",
               state="Checkout deep link shipped.")
    assert sync_quest_cards(store, discover_quest_folders(str(corpus)), str(corpus)) == 1
    assert "Checkout deep link shipped" in str(store.get_card(quest_card_id("quest_subs")))


def test_priority_card_is_selected_first_even_without_shared_words(corpus):
    from quest_ai_runner.runner.quest_folder_index import quest_card_id, sync_quest_cards
    store = make_store(corpus)
    sync_quest_cards(store, discover_quest_folders(str(corpus)), str(corpus))
    subs = quest_card_id("quest_subs")
    plain = store.assemble("tell me something unrelated about bananas")
    assert subs not in (plain.card_ids or [])
    forced = store.assemble("tell me something unrelated about bananas",
                            meta={"priority_card_ids": [subs]})
    assert forced.card_ids[0] == subs
    assert "Ship the checkout deep link" in forced.context_view
    # The cross-quest fence still wins: a turn scoped to another quest never gets this card.
    fenced = store.assemble("bananas", meta={"priority_card_ids": [subs],
                                             "scope_tags": ["quest:quest_wiki"]})
    assert subs not in (fenced.card_ids or [])


# -- session: auto, none (remembered), pinned, follow-ups ------------------------------------

def make_session(monkeypatch, corpus, tmp_path, env_default=None):
    from quest_ai_runner import interactive_session as mod
    from quest_ai_runner.config import RunnerConfig

    class FakeOrch:
        class cfg:
            instant_ack = False

    monkeypatch.setenv("QAR_CHAT_HISTORY_DIR", str(tmp_path / "convs"))
    monkeypatch.setenv("QAR_STATE_PATH", str(tmp_path / "qar_state.json"))
    if env_default is not None:
        monkeypatch.setenv("QAR_QUEST_AUTO_MATCH", env_default)
    else:
        monkeypatch.delenv("QAR_QUEST_AUTO_MATCH", raising=False)
    monkeypatch.setattr("quest_ai_runner.config.build_orchestrator", lambda cfg, notify=None: FakeOrch())
    monkeypatch.setattr(mod.InteractiveSession, "_build_model_tiers_menu", lambda self: None)
    monkeypatch.setattr(mod.InteractiveSession, "_try_load_skill_by_name", lambda self, n: None)
    monkeypatch.setattr(mod.InteractiveSession, "_refresh_rep_name_from_skill", lambda self: None)
    sess = mod.InteractiveSession(RunnerConfig(quest_base_url="", quest_api_key="",
                                               corpus_root=str(corpus)))
    lines = []
    sess._console.dim = lines.append
    return sess, lines


def test_matched_turn_puts_the_quest_card_first(monkeypatch, corpus, tmp_path):
    sess, _ = make_session(monkeypatch, corpus, tmp_path)
    preamble, meta = sess.turn_grounding("where is the 1000 subscribers quest")
    assert sess.turn_quest.quest_id == "quest_subs"
    assert meta == {"priority_card_ids": ["quest-folder-quest_subs"],
                    "quest_folder": str((corpus / "quest_subscribers_growth").resolve())}
    assert preamble is None  # the card carries it; no extra prompt text
    assert sess.turn_grounding("hello") == (None, None) and sess.turn_quest is None


def test_quest_without_local_folder_goes_in_the_preamble(monkeypatch, corpus, tmp_path):
    from quest_ai_runner.runner.quest_folder_index import quest_without_folder
    sess, _ = make_session(monkeypatch, corpus, tmp_path)
    sess.remote_quests = [quest_without_folder("quest_remote", "Launch the podcast series",
                                               "Two episodes recorded.")]
    sess.cmd_quest("quest_remote")
    preamble, meta = sess.turn_grounding("hi")
    assert meta is None and "Two episodes recorded." in preamble


def test_follow_up_stays_on_the_quest(monkeypatch, corpus, tmp_path):
    sess, _ = make_session(monkeypatch, corpus, tmp_path)
    sess._session_history.append(("how many paying subscribers do we have", "12"))
    sess.turn_grounding("and what is next for it?")
    assert sess.turn_quest.quest_id == "quest_subs"


def test_quest_none_turns_matching_off_and_is_remembered(monkeypatch, corpus, tmp_path):
    sess, lines = make_session(monkeypatch, corpus, tmp_path)
    sess.cmd_quest("none")
    assert sess.turn_grounding("where is the 1000 subscribers quest") == (None, None)
    assert json.loads((tmp_path / "qar_state.json").read_text())["chat_state"]["quest_match"] == "none"
    again, _ = make_session(monkeypatch, corpus, tmp_path)
    assert again.quest_match_mode == "none"
    again.cmd_quest("auto")
    again.turn_grounding("where is the 1000 subscribers quest")
    assert again.turn_quest.quest_id == "quest_subs"


def test_deployment_can_default_matching_off(monkeypatch, corpus, tmp_path):
    sess, _ = make_session(monkeypatch, corpus, tmp_path, env_default="0")
    assert sess.quest_match_mode == "none"
    assert sess.turn_grounding("where is the 1000 subscribers quest") == (None, None)


def test_pinned_quest_applies_to_every_turn(monkeypatch, corpus, tmp_path):
    sess, lines = make_session(monkeypatch, corpus, tmp_path)
    sess.cmd_quest("wikipedia")
    sess.turn_grounding("hello")
    assert sess.turn_quest.quest_id == "quest_wiki"
    sess.cmd_quest("nothing like any quest")
    assert any("No synced quest clearly matches" in l for l in lines)
    sess.cmd_quest("")
    assert any("Pinned:" in l for l in lines)


# -- shared-corpus file permissions ----------------------------------------------------------

def test_atomic_card_write_is_readable_by_others(tmp_path):
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
    old = os.umask(0o022)
    try:
        repo = FilesystemCardRepository(str(tmp_path))
        assert repo.write("card-a", {"id": "card-a"})
    finally:
        os.umask(old)
    mode = stat.S_IMODE((tmp_path / "card-a.json").stat().st_mode)
    assert mode & stat.S_IRGRP and mode & stat.S_IROTH, oct(mode)
