"""An autopilot pass carries what changed since it last looked, and closes the loop on it.

The wiring, end to end: the poller builds ONE ``UpdateEngine`` from the consumer's config, the pass
asks it once per quest and folds the answer into every batch that quest produces, the executor
turns the run's own account of what it used into a receipt on the result, and the watermark moves
only once a batch carrying the material was really created.

The compatibility test at the bottom is the one that matters most: a deployment with no engine
composes byte-identically to what it composed before any of this existed.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.autopilot import AutopilotPass, compose_batch_text
from quest_ai_runner.runner.context_updates import (
    BLOCK_START,
    ContextUpdate,
    UpdateEngine,
    Watermarks,
    build_update_engine,
)
from quest_ai_runner.runner.executor import TaskExecutor

from tests.test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _quest

NOW = datetime(2026, 9, 9, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


class NotingClient(FakeAutopilotClient):
    """The autopilot fake plus the quest-notes read the context engine uses."""

    def __init__(self, *args, notes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.notes = list(notes or [])

    def list_quest_notes(self, quest_id):
        return list(self.notes)


def _note(text, hours_ago=2, note_id="n1"):
    return {"id": note_id, "text": text, "author_kind": "user", "author_name": "the owner",
            "created_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


def _watching_quest(quest_id="q1", sources=("quest_notes",), **kwargs):
    quest = _quest(quest_id, **kwargs)
    quest["autopilot"]["context_sources"] = list(sources)
    return quest


def _engine(client, watermarks=None):
    return UpdateEngine(client, watermarks=watermarks or Watermarks(None),
                        always=(), now_fn=_now)


def _pass_with(client, engine, **kwargs):
    return AutopilotPass(client, team_id="team1", now=_now, update_engine=engine, **kwargs)


def _one_quest_client(notes):
    return NotingClient(
        quests=[_watching_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-09-09", [_goal("g1", "Draft ch. 2")]))},
        notes=notes,
    )


# --- the brief -------------------------------------------------------------------------------

def test_a_pass_carries_what_the_person_wrote_since_it_last_looked_into_the_batch():
    client = _one_quest_client([_note("The method chapter has to come first")])
    result = _pass_with(client, _engine(client)).run({"text": "autopilot pass"})
    assert result.created_task_ids
    text = client.created_tasks[0]["text"]
    assert BLOCK_START in text
    assert "The method chapter has to come first" in text
    # The channel back, and the account asked of the run.
    assert "add a note on this quest" in text
    assert "Context used:" in text


def test_a_quest_that_watches_nothing_composes_exactly_what_it_composed_before():
    client = NotingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-09-09", [_goal("g1", "Draft ch. 2")]))},
        notes=[_note("said something")],
    )
    _pass_with(client, _engine(client)).run({"text": "autopilot pass"})
    assert BLOCK_START not in client.created_tasks[0]["text"]


def test_a_pass_with_no_engine_wired_behaves_exactly_as_it_did_before_this_existed():
    def build(engine):
        client = _one_quest_client([_note("said something")])
        AutopilotPass(client, team_id="team1", now=_now, update_engine=engine).run(
            {"text": "autopilot pass"})
        return client.created_tasks[0]["text"]

    assert build(None) == build(None)
    assert BLOCK_START not in build(None)


def test_the_composer_is_byte_identical_without_a_context_updates_block():
    assert compose_batch_text("ship the thing") == \
        compose_batch_text("ship the thing", context_updates=None)


# --- the watermark, and what may move it --------------------------------------------------------

def test_the_watermark_moves_only_once_a_batch_carrying_the_material_was_created():
    client = _one_quest_client([_note("The method chapter has to come first")])
    marks = Watermarks(None)
    _pass_with(client, _engine(client, marks)).run({"text": "autopilot pass"})
    assert client.created_tasks
    assert marks.get("q1", "quest_notes") == NOW


def test_a_dry_run_reads_the_persons_note_without_consuming_it():
    client = _one_quest_client([_note("The method chapter has to come first")])
    marks = Watermarks(None)
    _pass_with(client, _engine(client, marks)).run({"text": "dry-run"})
    assert not client.created_tasks
    assert marks.get("q1", "quest_notes") is None


def test_a_pass_that_creates_nothing_leaves_the_note_for_the_next_one():
    """Today's budget was spent before this pass ran: the note was never handed to a run, so the
    next pass must still offer it."""
    client = _one_quest_client([_note("The method chapter has to come first")])
    client.tasks.append({"id": "t0", "task_kind": "autopilot_work", "goal_id": "other",
                         "status": "done", "created_at": "2026-09-09T01:00:00Z"})
    marks = Watermarks(None)
    result = _pass_with(client, _engine(client, marks), daily_budget=1).run(
        {"text": "autopilot pass"})
    assert result.created_task_ids == []
    assert not client.created_tasks
    assert marks.get("q1", "quest_notes") is None


def test_the_same_note_is_not_offered_twice_across_two_passes():
    client = _one_quest_client([_note("The method chapter has to come first")])
    marks = Watermarks(None)
    _pass_with(client, _engine(client, marks)).run({"text": "autopilot pass"})
    client.created_tasks.clear()
    _pass_with(client, _engine(client, marks)).run({"text": "autopilot pass"})
    assert BLOCK_START not in client.created_tasks[0]["text"]


def test_one_engine_serves_every_quest_in_a_pass():
    client = NotingClient(
        quests=[_watching_quest("q1"), _watching_quest("q2")],
        goals_by_quest={
            "q1": _goals_payload(("day", "2026-09-09", [_goal("g1", "Draft ch. 2")])),
            "q2": _goals_payload(("day", "2026-09-09", [_goal("g2", "Cut the release")])),
        },
        notes=[_note("The method chapter has to come first")],
    )
    _pass_with(client, _engine(client), daily_budget=5).run({"text": "autopilot pass"})
    assert len(client.created_tasks) == 2
    for task in client.created_tasks:
        assert "The method chapter has to come first" in task["text"]


def test_a_channel_that_cannot_be_read_never_stops_the_pass():
    class BrokenNotes(NotingClient):
        def list_quest_notes(self, quest_id):
            raise RuntimeError("the API said no")

    client = BrokenNotes(
        quests=[_watching_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-09-09", [_goal("g1", "Draft ch. 2")]))},
    )
    marks = Watermarks(None)
    result = _pass_with(client, _engine(client, marks)).run({"text": "autopilot pass"})
    assert result.created_task_ids            # the batch still went out
    assert marks.get("q1", "quest_notes") is None   # unread is not the same as seen


# --- the receipt, on the way back ----------------------------------------------------------------

class _ReceiptExecutor(TaskExecutor):
    """Only the receipt helper is under test here, so nothing else is constructed."""

    def __init__(self):  # noqa: D107 -- deliberately skips TaskExecutor.__init__
        pass


def _task_text_with(update):
    from quest_ai_runner.runner.context_updates import ContextUpdates
    bundle = ContextUpdates(card_id="q1", updates=[update])
    bundle.updates[0].ref = "U1"
    return "Work this batch.\n\n" + bundle.as_prompt_block()


def test_a_finished_run_reports_what_it_did_with_the_material_it_was_shown():
    task_text = _task_text_with(ContextUpdate(
        source="quest_notes", kind="note", item_id="n1", body="method first",
        occurred_at=NOW, location="the quest"))
    reported = _ReceiptExecutor()._with_context_receipt(
        "I rewrote the method section.\n\nContext used:\n  [U1] cited in the method", task_text)
    assert "Context updates taken into account:" in reported
    assert "-> cited in the method" in reported


def test_the_receipt_reads_the_runs_own_words_not_the_report_they_were_folded_into():
    """The fold-back that turns a worker transcript into a report is free to drop the usage lines;
    the receipt is the run's own account, so it is read from the raw output."""
    task_text = _task_text_with(ContextUpdate(
        source="quest_notes", kind="note", item_id="n1", body="method first",
        occurred_at=NOW, location="the quest"))
    reported = _ReceiptExecutor()._with_context_receipt(
        "A tidy summary with no usage lines at all.", task_text,
        run_output="raw transcript\n\nContext used:\n  [U1] answered in the doc")
    assert "-> answered in the doc" in reported
    assert "A tidy summary with no usage lines at all." in reported


def test_a_task_that_carried_no_updates_reports_exactly_what_it_would_have_reported():
    assert _ReceiptExecutor()._with_context_receipt("the work", "an ordinary task") == "the work"


# --- the consumer's switch ------------------------------------------------------------------------

def test_the_library_default_is_on_and_a_consumer_can_switch_it_off():
    assert RunnerConfig().context_updates is True
    assert build_update_engine(RunnerConfig(), None) is not None
    assert build_update_engine(RunnerConfig(context_updates=False), None) is None


def test_the_poller_hands_its_one_engine_to_the_pass_it_builds(tmp_path):
    """Where the feature actually switches on for a real deployment: an unconfigured runner gets an
    engine whose stamps persist beside its own state file, and a consumer that said no gets none."""
    from quest_ai_runner.runner.poller import Poller

    cfg = RunnerConfig(quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1")
    poller = Poller(cfg, state_path=str(tmp_path / "qar_state.json"))
    assert poller._update_engine is not None
    assert poller._autopilot._update_engine is poller._update_engine

    off = RunnerConfig(quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
                       context_updates=False)
    assert Poller(off, state_path=None)._autopilot._update_engine is None
