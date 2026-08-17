"""The person's reply reaches the next run, and can be told apart from the AI's own notes.

The loop this covers: a run produces something, the person answers it on the quest ("did the
reading, skipped the writing, do X instead"), and the NEXT run has to act on that. Three separate
faults broke it, each of them silent:

  1. The runner asked for notes at ``/api/teams/{t}/quests/{q}/goals/{g}/notes``, a route the
     reference backend does not implement. It 404s, ``list_goal_notes`` logs and returns [], and
     every run therefore built its context with NO notes while the person's replies sat unread on
     the quest. The collection that actually holds them is the quest's own (``list_quest_notes``).
  2. Notes went into the prompt as bare bullets with the author dropped, so a run could not tell
     the person's instruction from its own previous output -- on a goal it works daily, that is a
     dozen of its own summaries and one human line, indistinguishable.
  3. A plain "most recent N" window is dominated by AI notes on exactly those goals, so yesterday's
     human correction ages out while machine summaries stay.
"""
from __future__ import annotations

from quest_ai_runner.runner.executor import (
    PERSON_NOTE_FLOOR,
    REPLY_LOOP_CONTRACT,
    TaskExecutor,
    render_goal_notes,
)


def person(text: str, *, name: str = "Ada", when: str = "2026-08-14") -> dict:
    return {"text": text, "author_kind": "user", "author_name": name, "created_at": when}


def ai(text: str, *, name: str = "Ada", when: str = "2026-08-14") -> dict:
    """An AI-written note. ``author_name`` is the ACCOUNT OWNER's name, because the runner posts
    with their API key -- which is exactly why author_kind has to win when rendering."""
    return {"text": text, "author_kind": "ai", "author_name": name, "created_at": when}


class NotesClient:
    """The slice of QuestClient the context builder touches, with both note routes switchable."""

    def __init__(self, *, quest_notes=None, goal_notes=None, quest_notes_raises=False):
        self._quest_notes = quest_notes
        self._goal_notes = goal_notes
        self._quest_notes_raises = quest_notes_raises
        self.quest_notes_calls = []
        self.goal_notes_calls = []

    def list_quest_notes(self, quest_id):
        self.quest_notes_calls.append(quest_id)
        if self._quest_notes_raises:
            raise RuntimeError("404 Not Found")
        return list(self._quest_notes or [])

    def list_goal_notes(self, goal_id, *, quest_id=None, limit=None):
        self.goal_notes_calls.append((goal_id, quest_id, limit))
        return list(self._goal_notes or [])


def _executor(client) -> TaskExecutor:
    """A TaskExecutor with no orchestrator: ``_build_context_view`` never touches one."""
    return TaskExecutor(client, None)


# --- rendering: who said it -------------------------------------------------------

def test_the_persons_words_are_marked_as_theirs_and_the_ai_is_not():
    out = render_goal_notes([
        ai("Re-ran the sanity check; numbers match."),
        person("Skipped the writing yesterday, was sick. Start with the committee email."),
    ])

    assert "(Ada, the person) Skipped the writing" in out
    # The AI note carries the OWNER's name (the API key is theirs), so the "(AI)" marker is what
    # keeps it from reading as something Ada wrote.
    assert "(Ada (AI)) Re-ran the sanity check" in out
    # The run is told, in words, that the person's line outranks its own prior claims.
    assert "override" in out


def test_a_named_ai_persona_keeps_its_name_alongside_the_ai_marker():
    out = render_goal_notes([{"text": "Gap 5 resolved.", "author_kind": "ai",
                              "author_name": "Bailey", "created_at": "2026-08-14"}])

    assert "(Bailey (AI)) Gap 5 resolved." in out


def test_an_unattributed_note_is_never_claimed_to_be_the_person():
    """An older backend, or a note predating attribution, must not be asserted as an instruction."""
    out = render_goal_notes([{"text": "no author on this one", "created_at": "2026-08-14"}])
    note_line = [ln for ln in out.splitlines() if "no author on this one" in ln][0]

    assert "the person" not in note_line
    assert "(unattributed)" in note_line


def test_no_notes_renders_nothing():
    assert render_goal_notes([]) == ""
    assert render_goal_notes(None) == ""
    assert render_goal_notes([{"text": ""}]) == ""


# --- rendering: the person's note cannot be crowded out ---------------------------

def test_the_persons_note_survives_a_flood_of_ai_notes():
    """The live shape: one human correction, then a fortnight of daily AI summaries."""
    notes = [person("Committee first. The writing can wait.", when="2026-08-01")]
    notes += [ai(f"Daily summary {i}", when="2026-08-%02d" % (i + 1)) for i in range(2, 20)]

    out = render_goal_notes(notes)

    assert "Committee first" in out
    assert "Daily summary 19" in out          # recent AI context is still there
    assert "Daily summary 2" not in out       # ...but the oldest machine noise is dropped


def test_the_floor_is_bounded_and_chronological():
    notes = [person(f"reply {i}", when="2026-07-%02d" % (i + 1)) for i in range(6)]
    notes += [ai(f"summary {i}", when="2026-08-%02d" % (i + 1)) for i in range(12)]

    out = render_goal_notes(notes)
    kept = [ln for ln in out.splitlines() if "reply " in ln]

    assert len(kept) == PERSON_NOTE_FLOOR          # the most recent few, not every reply ever
    assert "reply 5" in kept[-1]                   # newest human note last
    assert out.index("reply 5") < out.index("summary 11")   # overall order stays chronological


# --- fetching: ask the collection that actually exists ----------------------------

def test_notes_come_from_the_quest_route_not_the_404ing_goal_route():
    client = NotesClient(quest_notes=[person("use the shorter format")])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1")

    assert client.quest_notes_calls == ["q1"]
    assert client.goal_notes_calls == []          # not even attempted when the quest route answers
    assert "use the shorter format" in view


def test_a_task_with_no_goal_still_gets_the_quest_notes():
    """Quest-level feedback is not goal-specific, so it must not depend on a goal being linked."""
    client = NotesClient(quest_notes=[person("stop emailing me at 6am")])
    view = _executor(client)._build_context_view(goal_id=None, quest_id="q1")

    assert "stop emailing me at 6am" in view


def test_the_per_goal_route_is_still_honored_as_a_fallback():
    """A backend that DOES implement per-goal notes keeps working."""
    client = NotesClient(quest_notes=[], goal_notes=[person("from the goal route")])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1")

    assert client.goal_notes_calls == [("g1", "q1", 8)]
    assert "from the goal route" in view


def test_a_failing_quest_route_falls_back_instead_of_losing_the_reply():
    client = NotesClient(quest_notes_raises=True, goal_notes=[person("still reaches the run")])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1")

    assert "still reaches the run" in view


def test_the_reply_loop_contract_rides_along_only_when_there_is_something_to_answer():
    with_notes = _executor(NotesClient(quest_notes=[person("x")]))._build_context_view(
        goal_id="g1", quest_id="q1")
    without = _executor(NotesClient(quest_notes=[]))._build_context_view(
        goal_id="g1", quest_id="q1")

    assert REPLY_LOOP_CONTRACT in with_notes
    assert REPLY_LOOP_CONTRACT not in without


def test_a_client_that_can_answer_nothing_yields_no_invented_context():
    class Bare:
        pass

    assert _executor(Bare())._build_context_view(goal_id="g1", quest_id="q1") == ""


# --- the email contract ---------------------------------------------------------

class EmailQuestClient(NotesClient):
    """A client whose quest reports whether email is switched on."""

    def __init__(self, *, email_enabled: bool, **kw):
        super().__init__(**kw)
        self._email_enabled = email_enabled

    def get_quest(self, quest_id, **kw):
        return {"quest_id": quest_id, "autopilot": {"email": {"enabled": self._email_enabled}}}


def test_a_run_on_an_email_quest_is_told_not_to_mail_by_hand():
    """Delivery is automatic once the result is recorded, so a hand-rolled send is a duplicate --
    and one that carries no reply address, which is the whole point of routing mail through Quest."""
    client = EmailQuestClient(email_enabled=True, quest_notes=[])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1", rep_id="bailey")

    assert "Email for this quest is ON" in view
    assert "do NOT send mail yourself" in view
    assert "send_quest_email --quest q1" in view
    assert "--rep bailey" in view              # signs as the persona that did the work
    assert "never" in view and "the audience" in view   # it picks the words, not the recipients


def test_a_quest_without_email_is_told_nothing_about_it():
    client = EmailQuestClient(email_enabled=False, quest_notes=[])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1")

    assert "send_quest_email" not in view


def test_the_command_still_works_without_a_persona():
    client = EmailQuestClient(email_enabled=True, quest_notes=[])
    view = _executor(client)._build_context_view(goal_id="g1", quest_id="q1")

    assert "send_quest_email --quest q1" in view
    assert "--rep" not in view


def test_a_client_that_cannot_report_quest_settings_never_breaks_the_run():
    view = _executor(NotesClient(quest_notes=[]))._build_context_view(goal_id="g1", quest_id="q1")

    assert "send_quest_email" not in view


# --- what earlier runs did ------------------------------------------------------

class HistoryClient(NotesClient):
    def __init__(self, *, tasks=None, **kw):
        super().__init__(**kw)
        self._tasks = tasks or []
        self.list_tasks_calls = []

    def list_tasks(self, **kw):
        self.list_tasks_calls.append(kw)
        return list(self._tasks)


def task(status: str, title: str, result: str = "", when: str = "2026-08-14") -> dict:
    return {"task_id": f"atask_{title}", "status": status, "title": title,
            "result": result, "updated_at": when}


def test_a_failed_run_is_still_reported_because_its_work_happened():
    """The live case: Friday's brief reported failed after mailing and writing notes, so Monday's
    run saw nothing and carried on as if the week had not started."""
    client = HistoryClient(tasks=[task("failed", "Friday brief", "Sent the brief; Gap 5 resolved.")])
    view = _executor(client)._build_context_view(goal_id=None, quest_id="q1")

    assert "Friday brief" in view
    assert "Gap 5 resolved" in view
    assert "may have written files, sent mail" in view      # failed != nothing happened


def test_history_is_asked_for_by_quest_and_not_filtered_to_successes():
    client = HistoryClient(tasks=[task("done", "a"), task("failed", "b")])
    _executor(client)._build_context_view(goal_id=None, quest_id="q1")

    assert client.list_tasks_calls == [{"goal_id": "q1"}]


def test_history_is_bounded_and_ends_with_the_most_recent():
    client = HistoryClient(tasks=[task("done", f"run{i}", when="2026-08-%02d" % (i + 1))
                                  for i in range(12)])
    view = _executor(client)._build_context_view(goal_id=None, quest_id="q1")

    assert "run11" in view          # newest kept
    assert "run0" not in view       # oldest dropped
    assert view.count("• [") <= 6


def test_a_run_with_predecessors_is_told_not_to_assume_the_person_acted():
    """Monday's brief moved on as though Friday's instructions had been followed. They had not."""
    view = _executor(HistoryClient(tasks=[task("done", "Friday brief", "asked him to write")])) \
        ._build_context_view(goal_id=None, quest_id="q1")

    assert "Do NOT assume the person did anything you asked for previously" in view
    assert "treat it as NOT done" in view


def test_a_quest_nothing_has_run_on_yet_is_not_told_to_doubt_progress():
    """The rule only means something where there IS a previously. With no earlier run, nobody has
    been asked to do anything, so this would only make a first run hedge about work never
    requested."""
    view = _executor(HistoryClient(tasks=[]))._build_context_view(goal_id=None, quest_id="q1")

    assert "Do NOT assume" not in view


def test_a_client_with_no_history_call_still_builds_context():
    view = _executor(NotesClient(quest_notes=[person("hi")]))._build_context_view(
        goal_id=None, quest_id="q1")

    assert "hi" in view
