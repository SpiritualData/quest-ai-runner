"""This package's conversation drawn by the hub-selected opencode terminal: the harness (a turn is a run), the header, the slash commands and the screen switch. Offline, no model, no client."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from quest_ai_runner.the_consciousness.terminal import cli_chat
from quest_ai_runner.the_consciousness.terminal.opencode_terminal import AUTO, Owner, QuestCommands, QuestSessionHarness
from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult


class Inbox:
    def __init__(self):
        self.items = []

    def push(self, key, text):
        self.items.append(text)

    def drain(self, key):
        out, self.items = self.items, []
        return out

    def peek(self, key):
        return list(self.items)


class Orchestrator:
    def __init__(self, events, final):
        self.events, self.final, self.input_inbox, self.calls = events, final, Inbox(), []

    def run_stream(self, goal, **kwargs):
        self.calls.append((goal, kwargs))
        yield from self.events
        yield self.final


QUEST = SimpleNamespace(quest_id="quest_1", title="Garden", folder="/tmp/garden", current_state="")


class Session:
    def __init__(self, orch):
        self._orch, self._model_hint, self._replan_next = orch, None, False
        self._persona, self._persona_file, self._rep_name = "p", None, "Zee"
        self.pinned_quest, self.turn_quest, self.quest_match_mode = None, None, "auto"
        self._session_history, self._turn_count, self._conv_id = [], 0, "qar_chat_abc"
        self._last_user = self._last_assistant = ""
        self._shown_by_turn, self._work_by_turn = {}, {}
        self.saved = 0

    def turn_grounding(self, text):
        self.turn_quest = QUEST
        return "preamble", {"quest_folder": "/tmp/garden"}

    def bound_quest_id(self):
        return None

    def _last_transcript(self):
        return ""

    def _write_session_file(self):
        self.saved += 1

    def _maybe_refresh_next_steps(self, final):
        pass

    def _persist_session_state(self):
        pass

    def _cmd_model(self, arg):
        self._model_hint = arg or None
        self._console.dim(f"Model set to {arg}.")

    def quest_choices(self):
        return [QUEST]

    def cmd_quest(self, arg):
        self._console.dim(f"quest {arg!r}")


def owner_with(session):
    owner = Owner(None, rep_name="Zee", persona=None, goal_id=None, rep_specified=True, persona_specified=True, factory=lambda: session)
    owner.ready.wait(5)
    return owner


def run_turn(events, final, goal="what is next for the garden?"):
    session = Session(Orchestrator(events, final))
    owner = owner_with(session)
    said, sent = [], []
    owner.say = lambda lines: said.append(lines)
    harness = QuestSessionHarness(owner)
    done = harness.start(goal, sent.append)
    return session, owner, said, sent, done


def test_a_turn_is_one_run_that_ends_with_the_answer_and_is_kept_like_the_textual_screens_turn():
    events = [SimpleNamespace(type="partial", text="Watering ", data={}), SimpleNamespace(type="partial", text="is next.", data={})]
    session, owner, said, sent, done = run_turn(events, OrchestratorResult(kind="answer", text="Watering is next."))
    assert done.outcome == "met" and "Watering is next." in done.report and done.session_id == "abc"
    assert [e.type for e in sent][0] == "run-started"
    assert session._session_history == [("what is next for the garden?", "Watering is next.")] and session.saved == 1
    assert any("Matched quest: Garden" in line for line in said)
    assert session._orch.calls[0][1]["rep_preamble"] == "preamble" and session._orch.calls[0][1]["context_meta"] == {"quest_folder": "/tmp/garden"}


def test_the_saved_notes_line_is_said_once_and_kept_for_the_header():
    context = SimpleNamespace(type="context", text="", data={"card_metadata": [{"title": "Tomatoes", "score": 1}, {"title": "Soil", "score": 0.5}], "sources": []})
    session, owner, said, sent, done = run_turn([context], OrchestratorResult(kind="answer", text="ok"))
    assert any(line.startswith("Looked at 2 sources: Tomatoes") for line in said)
    assert owner.header()[-1].startswith("Looked at 2 sources")


def test_the_header_names_the_quest_the_persona_and_the_model():
    session = Session(Orchestrator([], None))
    owner = owner_with(session)
    assert owner.header()[:2] == ["Quest: matched per message", "Persona: loaded"] and owner.header()[-1] == "Model: auto"
    session.pinned_quest, session._model_hint = QUEST, "fast"
    assert owner.header()[0] == "Quest: Garden (selected)" and "Model: fast" in owner.header()


def test_a_question_waits_for_the_next_typed_line_and_is_not_a_permission_prompt():
    asked = SimpleNamespace(type="decision", text="Which bed?", data={}, decision_id="d1")
    session, owner, said, sent, done = run_turn([asked], OrchestratorResult(kind="confirm", text="", decision_id="d1", question="Which bed?"))
    assert done.outcome == "unmet" and "Which bed?" in done.report and not [e for e in sent if e.type == "waiting"]


def test_a_stop_ends_the_turn_stopped_and_keeps_the_marker():
    session = Session(Orchestrator([], OrchestratorResult(kind="answer", text="late")))
    owner = owner_with(session)
    harness = QuestSessionHarness(owner)
    original = session._orch.run_stream

    def stopping(goal, **kwargs):
        harness.control(__import__("hubofhubs.api", fromlist=["Stop"]).Stop())
        return original(goal, **kwargs)
    session._orch.run_stream = stopping
    done = harness.start("go", lambda event: None)
    assert done.outcome == "stopped" and session._session_history == [("go", "[cancelled by user]")]


def test_a_steering_note_goes_to_the_sessions_inbox_and_is_said_taken_up_when_the_worker_confirms():
    from hubofhubs.api import Steer
    session = Session(Orchestrator([], OrchestratorResult(kind="answer", text="ok")))
    harness = QuestSessionHarness(owner_with(session))
    assert harness.capabilities.steer and harness.control(Steer(text="water first", message_id="m1"))
    assert session._orch.input_inbox.items == ["water first"] and harness.sent == {"water first": "m1"}


def test_a_failed_model_call_is_said_in_plain_words_and_the_session_goes_on():
    session = Session(Orchestrator([], None))
    session._orch.run_stream = lambda goal, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    owner = owner_with(session)
    done = QuestSessionHarness(owner).start("go", lambda event: None)
    assert done.outcome == "failed" and "couldn't answer" in done.report


def test_the_slash_commands_run_the_sessions_handlers_and_say_their_lines():
    session = Session(Orchestrator([], None))
    owner = owner_with(session)
    picked = []
    owner.select_picker = lambda tier: picked.append(tier) or True
    by_name = {c.name: c for c in QuestCommands(owner).specs()}
    assert {"quest", "rep", "reps", "model", "save", "sessions", "whoami", "status", "tasks", "clear", "help"} <= set(by_name) and "quit" not in by_name
    assert by_name["quest"].description and by_name["model"].run("fast") == ["Model set to fast."] and picked == ["fast"]
    assert by_name["quest"].run("garden") == ["quest 'garden'"]
    listed = by_name["quest"].run("")
    assert listed[0] == "quest ''" and any("Garden" in line for line in listed)
    session._session_history.append(("a", "b"))
    by_name["clear"].run("")
    assert session._session_history == []
    session._skills_dir = lambda: "/nonexistent"
    assert "No SKILL.md files" in by_name["reps"].run("")[0]


def test_the_screen_follows_the_hub_selected_terminal_and_the_switch_keeps_the_textual_screen():
    assert cli_chat.resolve_screen("chat", {}) == "chat" and cli_chat.resolve_screen("opencode", {}) == "opencode" and cli_chat.resolve_screen("hub", {}) == "hub"
    assert cli_chat.resolve_screen(None, {"QAR_SCREEN": "chat"}) == "chat"
    assert cli_chat.resolve_screen(None, {"PATH": "/nonexistent"}) == "chat"      # no client installed: the screen that always works
    assert AUTO == "auto"


def test_python_warnings_go_to_a_file_not_the_terminal(capsys):
    import warnings
    from quest_ai_runner.the_consciousness.terminal.cli_chat import keep_warnings_off_the_screen
    saved = warnings.showwarning
    try:
        path = keep_warnings_off_the_screen()
        warnings.warn("Qdrant client version 1.19.0 is incompatible with server version 1.15.5", UserWarning)
        assert capsys.readouterr().err == ""
        assert "incompatible" in open(path).read()
    finally:
        warnings.showwarning = saved
