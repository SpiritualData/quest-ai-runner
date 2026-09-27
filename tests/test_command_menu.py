"""The "/" menu: typing "/" lists commands with descriptions, like Claude Code, and "/quest "
lists the quests a person can pick (plus turning matching off or back on)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from quest_ai_runner.command_menu import menu_items, parse_help
from quest_ai_runner.interactive_session import _HELP
from quest_ai_runner.runner.quest_folder_index import quest_without_folder

COMMANDS = parse_help(_HELP)


def labels(items):
    return [i.label for i in items]


def test_help_is_parsed_into_commands_with_descriptions():
    by_label = {c.label: c for c in COMMANDS}
    assert by_label["/help"].description == "Show this help"
    assert by_label["/model [tier]"].completion == "/model" and by_label["/model [tier]"].submit
    assert by_label["/rep <name>"].completion == "/rep " and not by_label["/rep <name>"].submit
    assert by_label["/quest none"].completion == "/quest none" and by_label["/quest none"].submit
    assert by_label["/quit, /q"].completion == "/quit"


def test_slash_lists_everything_and_typing_narrows():
    assert len(menu_items("/", COMMANDS)) > 5
    assert labels(menu_items("/ses", COMMANDS)) == ["/sessions"]
    assert "/q" not in labels(menu_items("/q", COMMANDS))  # shown once, as "/quit, /q"
    assert any(l.startswith("/quit") for l in labels(menu_items("/q", COMMANDS)))
    assert menu_items("hello", COMMANDS) == []
    assert menu_items("/save notes", COMMANDS) == []  # typing an argument: nothing to suggest


def test_quest_choices_filter_and_mark_current():
    quests = [SimpleNamespace(quest_id="quest_subs", title="Reach 1000 paying subscribers",
                              folder="/corpus/quest_subscribers_growth"),
              quest_without_folder("quest_wiki", "Wikipedia representation")]
    items = menu_items("/quest ", COMMANDS, quests, mode="none")
    assert labels(items) == ["none", "auto", "Reach 1000 paying subscribers", "Wikipedia representation"]
    assert "(current)" in items[0].description
    assert items[3].description == "not synced to a local folder"
    assert items[2].completion == "/quest quest_subs"
    assert labels(menu_items("/quest wiki", COMMANDS, quests)) == ["Wikipedia representation"]
    pinned = menu_items("/quest ", COMMANDS, quests, pinned_id="quest_wiki")
    assert "(pinned)" in pinned[3].description


def run_app(keys, quests=()):
    from quest_ai_runner.textual_ui import QuestAITerminal

    ran = []

    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            app.sess = SimpleNamespace(quest_choices=lambda: list(quests), quest_match_mode="auto",
                                       pinned_quest=None, cmd_quest=lambda arg: None)
            app._dispatch_command = ran.append
            await pilot.press(*keys)
            await pilot.pause()
            menu = app.query_one("#command-menu")
            prompt = app.query_one("#prompt")
            return menu.display, prompt.text, ran, [i.label for i in app.menu_items_shown]

    return asyncio.run(go())


def test_typing_slash_opens_the_menu():
    shown, text, ran, items = run_app(["slash"])
    assert shown and text == "/" and "/help" in items and ran == []


def test_enter_runs_the_highlighted_command():
    shown, _, ran, _ = run_app(["slash", "s", "e", "s", "enter"])
    assert ran == ["/sessions"] and not shown


def test_tab_completes_and_arguments_continue_into_quest_choices():
    quests = [quest_without_folder("quest_wiki", "Wikipedia representation")]
    # "/quest <name>" needs an argument: Enter completes it, then the quest list appears.
    shown, text, ran, items = run_app(list("/quest") + ["down", "down", "down", "tab"], quests)
    assert text == "/quest " and ran == [] and shown
    assert items == ["none", "auto", "Wikipedia representation"]
    shown, text, ran, items = run_app(list("/quest w") + ["enter"], quests)
    assert ran == ["/quest quest_wiki"]


def test_escape_closes_the_menu_and_keeps_the_text():
    shown, text, ran, _ = run_app(["slash", "h", "escape"])
    assert not shown and text == "/h" and ran == []
