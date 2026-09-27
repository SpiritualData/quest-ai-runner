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
    # The quests themselves lead; "Match automatically" and "No quest" come last.
    assert labels(items) == ["Reach 1000 paying subscribers", "Wikipedia representation",
                             "Match automatically", "No quest"]
    assert "(current)" in items[3].description
    assert items[1].description == "not synced to a local folder"
    assert items[0].completion == "/quest quest_subs"
    assert labels(menu_items("/quest wiki", COMMANDS, quests)) == ["Wikipedia representation"]
    pinned = menu_items("/quest ", COMMANDS, quests, pinned_id="quest_wiki")
    assert "(selected)" in pinned[1].description


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
    shown, text, ran, items = run_app(list("/quest") + ["down", "tab"], quests)
    assert text == "/quest " and ran == [] and shown
    assert items == ["Wikipedia representation", "Match automatically", "No quest"]
    shown, text, ran, items = run_app(list("/quest w") + ["enter"], quests)
    assert ran == ["/quest quest_wiki"]


def test_escape_closes_the_menu_and_keeps_the_text():
    shown, text, ran, _ = run_app(["slash", "h", "escape"])
    assert not shown and text == "/h" and ran == []


# -- choosing an option: /model, /quest open a chooser, not a printed list ------------------

def run_chooser(keys, quests=()):
    from quest_ai_runner.textual_ui import QuestAITerminal

    calls = {"quest": [], "persisted": 0}

    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            sess = SimpleNamespace(
                quest_choices=lambda: list(quests), quest_match_mode="auto", pinned_quest=None,
                cmd_quest=calls["quest"].append, _model_hint=None,
                _model_tiers=[("auto", "orchestrator decides"), ("haiku", "fast"),
                              ("sonnet", "balanced"), ("opus", "best")],
                _persist_session_state=lambda: calls.__setitem__("persisted", calls["persisted"] + 1),
            )
            app.sess = sess
            await pilot.press(*keys)
            await pilot.pause()
            return sess, calls, app.query_one("#command-menu").display, app.choice_picker

    return asyncio.run(go())


def test_model_opens_a_chooser_and_arrows_enter_select():
    sess, calls, shown, picker = run_chooser(list("/model") + ["enter", "down", "down", "enter"])
    assert sess._model_hint == "sonnet" and calls["persisted"] == 1
    assert not shown and picker is None


def test_chooser_typing_narrows_and_a_number_still_works():
    sess, _, _, _ = run_chooser(list("/model") + ["enter"] + list("opu") + ["enter"])
    assert sess._model_hint == "opus"
    sess, _, _, _ = run_chooser(list("/model") + ["enter", "2", "enter"])
    assert sess._model_hint == "haiku"


def test_escape_cancels_the_chooser():
    sess, calls, shown, picker = run_chooser(list("/model") + ["enter", "down", "escape"])
    assert sess._model_hint is None and calls["persisted"] == 0 and picker is None and not shown


def test_bare_quest_opens_a_chooser_and_pins_the_pick():
    quests = [quest_without_folder("quest_wiki", "Wikipedia representation")]
    _, calls, _, picker = run_chooser(list("/quest") + ["enter", "enter"], quests)
    assert calls["quest"] == ["quest_wiki"] and picker is None
    _, calls, _, _ = run_chooser(list("/quest") + ["enter", "down", "down", "enter"], quests)
    assert calls["quest"] == ["none"]


def test_quest_chooser_fills_in_when_the_quest_list_arrives():
    from quest_ai_runner.textual_ui import QuestAITerminal
    later = []

    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            app.sess = SimpleNamespace(quest_choices=lambda: list(later), quest_match_mode="auto",
                                       pinned_quest=None, cmd_quest=lambda a: None,
                                       no_quests_reason=lambda: "none yet")
            await pilot.press(*list("/quest"), "enter")
            await pilot.pause()
            before = [i.label for i in app.menu_items_shown]
            later.append(quest_without_folder("quest_course", "Get paid registrations for the course"))
            app.pick_quest(True)
            await pilot.pause()
            return before, [i.label for i in app.menu_items_shown]

    before, after = asyncio.run(go())
    assert before == ["Match automatically", "No quest"]
    assert after[0] == "Get paid registrations for the course"


# -- a "/" line is never handed to the AI ----------------------------------------------------

def test_slash_command_during_a_turn_runs_instead_of_going_to_the_ai():
    from quest_ai_runner.textual_ui import QuestAITerminal
    pushed = []

    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            app.sess = SimpleNamespace(
                quest_choices=lambda: [], quest_match_mode="auto", pinned_quest=None,
                cmd_quest=lambda arg: None, no_quests_reason=lambda: "none here",
                _orch=SimpleNamespace(input_inbox=SimpleNamespace(push=lambda sid, l: pushed.append(l))))
            app._turn_active = True
            await pilot.press(*list("/quest"), "enter")
            await pilot.pause()
            return app.choice_picker

    picker = asyncio.run(go())
    assert picker is not None and pushed == []


def test_command_typed_while_loading_runs_as_a_command():
    from quest_ai_runner.textual_ui import QuestAITerminal
    dispatched, turns = [], []

    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            app._dispatch_command = dispatched.append
            app._begin_turn = lambda line, echo=True, auto=False: turns.append(line)
            app._pre_session_queue.extend(["/model", "hello"])
            app._finish_startup(SimpleNamespace(
                _rep_name="Tester", _cfg=SimpleNamespace(corpus_root=None), _goal_id=None,
                _model_hint=None, _console=None, resumed=None))
            await pilot.pause()

    asyncio.run(go())
    assert dispatched == ["/model"] and turns == ["hello"]
