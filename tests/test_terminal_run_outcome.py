"""A turn's ending is said once, truthfully, in plain words, and a failed start is not retried.

An independent review of the terminal chat found a turn that printed a green "Deep task complete" for
every outcome that was not an error (a usage-limit pause, a question waiting for an answer), retried
by itself after the worker could not even start, printed three separate "done" lines for one goal and
the goal itself five times, showed a question with no speaker as a mid-transcript line under later
"done" lines, and showed a raw provider error with no way forward.

These tests drive the real app headless (``run_test``) with stand-in results. Every decision they pin
reads a structured field of the result (``met``, ``launch_failed``, ``usage_limited``, ``decision_id``,
``kind``, an event's ``phase``), never the words of an output: a result whose TEXT talks about a limit or
a failed start, with those fields unset, must read as the plain "stopped before finishing".
"""
from __future__ import annotations

import ast
import asyncio
import glob
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quest_ai_runner.core import usage_limit
from quest_ai_runner.core.adapters import (
    DeepResult, EVENT_DECISION, EVENT_EXEC, EVENT_MILESTONE, LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED,
)
from quest_ai_runner.core.orchestrator import OrchestratorResult
from quest_ai_runner import textual_ui_outcome as outcome
from quest_ai_runner import textual_ui_turn
from quest_ai_runner.textual_ui import QuestAITerminal
from quest_ai_runner.textual_ui_widgets import _AWAITING_DECISION_PLACEHOLDER

REPO = Path(__file__).resolve().parent.parent
GOAL = "Add the opportunities to the spreadsheet"
LIMIT_REFUSAL = "You've hit your weekly limit · resets 1pm (America/Los_Angeles)"
NOW = datetime(2026, 10, 21, 14, 3, tzinfo=timezone.utc)


@pytest.fixture
def utc(monkeypatch):
    """The host's clock is UTC, so a reset time reads the same on every machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class Log:
    """Everything written to the transcript, as plain lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(x.plain if hasattr(x, "plain") else getattr(x, "markup", str(x)))

    def stripped(self) -> list[str]:
        return [ln.strip() for ln in self.lines]


class Console:
    def __init__(self, log: Log) -> None:
        self.log = log

    def markdown(self, text: str) -> None:
        self.log.lines.append(text)

    def dim(self, text: str) -> None:
        self.log.lines.append(text)

    def rule(self) -> None:
        self.log.lines.append("-----")


class Session:
    """The part of a session ``_finish_turn`` touches; a deep runner is wired."""
    _rep_name = "Tester"
    _goal_id = None
    _model_hint = None

    def __init__(self, deep_runner=object()) -> None:
        self._cfg = SimpleNamespace(corpus_root=None, deep_runner=deep_runner)
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self._orch = SimpleNamespace(context_assembler=None, input_inbox=None)

    def _write_session_file(self) -> None:
        pass

    def _maybe_refresh_next_steps(self, final) -> None:
        pass


def deep_final(*results: DeepResult, goals=(GOAL,)) -> OrchestratorResult:
    return OrchestratorResult(kind="deep", goals=list(goals), deep_results=list(results), steps=2)


MET = dict(met=True, output="Added 14 rows.", model="sonnet", resolved_model="claude-sonnet-5-5", tokens=18200)


def run_turn(final, *, error=None, elapsed=89.0, verbosity=0, session=None, pass_number=1, before=None):
    """Finish one turn on a mounted app and return (log, begun, app state): ``begun`` is every
    ``_begin_turn`` it started (an automatic second pass shows up there)."""
    async def go():
        app = QuestAITerminal(session or Session(), verbosity)
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = Log()
            app._tlog = log
            app._console = Console(log)
            begun = []
            app._begin_turn = lambda *a, **k: begun.append((a, k))
            app._auto_pass = pass_number
            app._turn_active = True
            if before is not None:
                before(app)
            app._finish_turn("add the rows", final, elapsed, cancelled=False, error=error)
            await pilot.pause()
            state = SimpleNamespace(placeholder=app.query_one("#prompt").placeholder,
                                    prompt_text=app.query_one("#prompt").text,
                                    awaiting=app._awaiting_decision)
            return log, begun, state

    return asyncio.run(go())


# --- how a turn ended: structured fields only ----------------------------------------------

def test_outcome_per_result():
    met = DeepResult(**MET)
    assert outcome.turn_outcome(deep_final(met)) == outcome.OUTCOME_FINISHED
    assert outcome.turn_outcome(deep_final(met, DeepResult(met=False, output="half"))) == outcome.OUTCOME_STOPPED
    assert outcome.turn_outcome(deep_final(DeepResult(met=False, launch_failed=True))) == outcome.OUTCOME_NOT_STARTED
    assert outcome.turn_outcome(deep_final(DeepResult(met=False, usage_limited=True))) == outcome.OUTCOME_LIMIT
    assert outcome.turn_outcome(deep_final()) == outcome.OUTCOME_NOT_RUN
    assert outcome.turn_outcome(deep_final(DeepResult(met=False, decision_id="d1"))) == outcome.OUTCOME_WAITING
    assert outcome.turn_outcome(OrchestratorResult(kind="confirm", question="Which one?")) == outcome.OUTCOME_WAITING
    assert outcome.turn_outcome(OrchestratorResult(kind="answer", text="Hi.")) is None
    # An answer turn whose deferred work parked on an approval is waiting too.
    assert outcome.turn_outcome(OrchestratorResult(kind="answer", text="Parked.", decision_id="d2")) == outcome.OUTCOME_WAITING


def test_the_words_of_an_output_never_decide_the_outcome():
    """Hard rule #3: a result that TALKS about a limit, a failed start or a question, with its structured
    fields unset, is simply a run that stopped before finishing."""
    talky = DeepResult(met=False, output="You've hit your weekly limit. The deep worker could not start. Should I go on?",
                       error="The deep worker could not start: usage limit reached")
    final = deep_final(talky)
    assert outcome.turn_outcome(final) == outcome.OUTCOME_STOPPED
    assert outcome.launch_failure_lines(final) == []
    assert outcome.allows_second_pass(final)


@pytest.mark.parametrize("flag", ["launch_failed", "usage_limited", "paused", "cancelled"])
def test_no_second_pass_after_a_start_failure_a_limit_a_pause_or_a_cancel(flag):
    assert not outcome.allows_second_pass(deep_final(DeepResult(met=False, **{flag: True})))
    assert not outcome.allows_second_pass(deep_final(DeepResult(met=False, decision_id="d1")))
    assert not outcome.allows_second_pass(OrchestratorResult(kind="confirm", question="Which?"))
    assert outcome.allows_second_pass(deep_final(DeepResult(met=False)))


def test_the_closing_text_per_outcome():
    assert outcome.closing_text(outcome.OUTCOME_FINISHED) == ("✓ Done", "green")
    assert outcome.closing_text(outcome.OUTCOME_STOPPED)[0] == "~ Stopped before finishing"
    assert outcome.closing_text(outcome.OUTCOME_NOT_STARTED)[0] == "✗ Nothing ran"
    assert outcome.closing_text(outcome.OUTCOME_NOT_RUN)[0] == "~ Nothing was run"
    assert outcome.closing_text(outcome.OUTCOME_WAITING)[0] == "Waiting for your answer"
    assert outcome.closing_text(outcome.OUTCOME_LIMIT, "at 15:40 UTC")[0] == (
        "Paused at Claude Code's usage limit. It resets at 15:40 UTC.")
    assert outcome.closing_text(outcome.OUTCOME_LIMIT)[0] == (
        "Paused at Claude Code's usage limit. It has not said when it resets.")


# --- the one closing line --------------------------------------------------------------------

def closing_line(log: Log) -> str:
    return next(ln for ln in log.stripped() if ln.startswith(("✓", "~", "✗", "Paused", "Waiting")))


def test_a_finished_goal_ends_with_one_quiet_line_and_no_other_done_marker():
    log, begun, _ = run_turn(deep_final(DeepResult(**MET)), elapsed=212.0, before=lambda app: (
        app._future_ctx_panel.load("- card: Pricing"), setattr(app, "_future_context", "- card: Pricing")))
    assert closing_line(log) == "✓ Done in 3m32s · sonnet · Alt+C context used"
    assert [ln for ln in log.stripped() if ln.startswith("✓")] == ["✓ Done in 3m32s · sonnet · Alt+C context used"]
    assert not begun
    body = "\n".join(log.lines)
    assert "complete" not in body.lower() and "Partial" not in body and "tokens" not in body


def test_the_model_is_the_short_alias_once_and_the_full_id_only_with_v():
    quiet, _, _ = run_turn(deep_final(DeepResult(**MET), DeepResult(**MET)))
    assert closing_line(quiet) == "✓ Done in 1m29s · sonnet"
    assert "claude-sonnet-5-5" not in "\n".join(quiet.lines)
    verbose, _, _ = run_turn(deep_final(DeepResult(**MET)), verbosity=1)
    assert closing_line(verbose) == "✓ Done in 1m29s · 18.2k tokens · sonnet (claude-sonnet-5-5)"


def test_a_run_that_stopped_before_finishing_says_so_and_never_claims_done():
    log, begun, _ = run_turn(deep_final(DeepResult(met=False, output="Wrote 6 of 14 rows.", model="sonnet")))
    assert closing_line(log) == "~ Stopped before finishing · 1m29s · sonnet"
    assert not any(ln.startswith("✓") for ln in log.stripped())
    assert not begun, "output exists, so nothing is retried"


def test_a_usage_limit_pause_says_when_it_resets_and_is_not_retried(monkeypatch):
    monkeypatch.setattr(textual_ui_turn, "limit_reset", lambda final: "at 20:00 UTC")
    log, begun, _ = run_turn(deep_final(DeepResult(met=False, usage_limited=True, error=LIMIT_REFUSAL, model="sonnet")))
    assert closing_line(log) == "Paused at Claude Code's usage limit. It resets at 20:00 UTC. · 1m29s · sonnet"
    assert not any(ln.startswith("✓") for ln in log.stripped())
    assert not begun, "no automatic second pass at a usage limit"
    assert "Task identified" not in "\n".join(log.lines), "no plan is printed for work that was refused"


def test_the_reset_time_is_read_from_the_limit_the_run_reported_never_guessed(utc):
    usage_limit.reset_for_tests()
    final = deep_final(DeepResult(met=False, usage_limited=True, error=LIMIT_REFUSAL))
    assert outcome.limit_reset(final, NOW) == "at 20:00 UTC", "1pm in Los Angeles, in the host's own zone"
    # A refusal with no reset in it, and no lane note: nothing is invented.
    bare = deep_final(DeepResult(met=False, usage_limited=True, error="You've hit your usage limit"))
    assert outcome.limit_reset(bare, NOW) == ""
    # With the lane's own note of the limit (what the runner records when it hits it), that time is used.
    usage_limit.record(usage_limit.detect_usage_limit(LIMIT_REFUSAL, NOW))
    try:
        assert outcome.limit_reset(bare, NOW) == "at 20:00 UTC"
    finally:
        usage_limit.reset_for_tests()


# --- a run that never started -----------------------------------------------------------------

STARTFAIL = DeepResult(met=False, launch_failed=True, launch_problem=LAUNCH_MISSING_PROGRAM,
                       error="The deep worker could not start: binary not found (claude).")


def test_a_worker_that_could_not_start_is_explained_in_the_transcript_and_not_retried():
    log, begun, _ = run_turn(deep_final(STARTFAIL), elapsed=7.0)
    body = log.stripped()
    assert "Tester (AI):" in body
    assert body[body.index("Tester (AI):") + 1:body.index("Tester (AI):") + 4] == [
        "I couldn't start the work, so nothing was changed.",
        "The reason: the program that runs the work is not installed on this machine, or is not on its PATH.",
        "Fix that, then send your request again."]
    assert closing_line(log) == "✗ Nothing ran · 7s"
    assert not begun, "a start failure would fail the same way again: no automatic pass"
    assert "deep worker" not in "\n".join(log.lines), "customer wording: the work, not the deep worker"
    assert "Task identified" not in "\n".join(log.lines)


def test_the_cli_error_text_is_not_shown_as_the_answer_of_a_run_that_never_started():
    failed = DeepResult(met=False, launch_failed=True, output="There's an issue with the selected model.",
                        error="The deep worker could not start: There's an issue with the selected model.")
    log, _, _ = run_turn(deep_final(failed))
    assert "There's an issue with the selected model." in "\n".join(log.lines), "it is the stated reason"
    assert sum("There's an issue" in ln for ln in log.lines) == 1, "once, as the reason, not also as an answer"


def test_the_launch_failure_wording_matches_what_the_runners_write(tmp_path):
    """The cause is the runner's sentence minus its stock opening, so the opening must be the one the
    runners write (the quest mailer keys on the same words)."""
    from quest_ai_runner.core.goal_runner import SubprocessGoalRunner, SubprocessConfig
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=str(tmp_path / "no-such-claude")))
    res = runner.run_goal(goal="g", brief="g")
    assert res.launch_failed and res.error.startswith(outcome.LAUNCH_FAILED_PREFIX)
    assert outcome.launch_failure_cause(res.error) == res.error[len(outcome.LAUNCH_FAILED_PREFIX):].lstrip(": ")
    assert outcome.launch_failure_cause("") == "no reason was given"
    assert outcome.launch_failure_cause("Something else broke.") == "Something else broke."


# --- the automatic second pass ------------------------------------------------------------------

def test_a_plan_that_never_ran_gets_one_automatic_second_pass_with_its_reason_stated():
    log, begun, _ = run_turn(deep_final(), pass_number=1)
    assert len(begun) == 1 and begun[0][1].get("auto") is True
    assert "↻ Nothing ran the first time, so I'm trying once more." in log.stripped()
    assert not any("Pass" in ln for ln in log.lines), "no 'pass 2' jargon"


def test_there_is_never_a_third_pass_and_an_unexecuted_result_after_the_second_ends_calmly():
    log, begun, _ = run_turn(deep_final(), pass_number=outcome.MAX_AUTO_PASSES)
    assert begun == []
    assert closing_line(log) == "~ Nothing was run · 1m29s"


def test_a_missing_executor_is_never_retried():
    log, begun, _ = run_turn(deep_final(), session=Session(deep_runner=None))
    assert begun == []


# --- a question that waits for the person ----------------------------------------------------------

QUESTION = "14 of the opportunities are on the exclusion list. Add them anyway, or skip them?"


def decision_final() -> OrchestratorResult:
    return OrchestratorResult(kind="confirm", question=QUESTION, decision_id="dec_1", steps=3)


def test_a_question_ends_the_turn_as_the_last_block_with_a_speaker():
    def before(app):
        app._handle_event({"type": EVENT_DECISION, "text": QUESTION, "decision_id": "dec_1"})

    log, begun, state = run_turn(decision_final(), before=before)
    lines = log.stripped()
    last_text = [ln for ln in lines if ln and ln != "-----"]
    assert last_text[-2:] == ["Tester (AI):", f"┃ {QUESTION}"], "the question is the last thing, from its speaker"
    assert lines.count(f"┃ {QUESTION}") == 1, "written once"
    assert "Waiting for your answer" in closing_line(log)
    assert not any(ln.startswith("✓") for ln in lines), "no done tick under a question"
    assert state.placeholder == _AWAITING_DECISION_PLACEHOLDER == "Type your answer to the question above…"
    assert state.awaiting and not begun


def test_a_question_that_arrived_as_the_result_only_is_still_written():
    log, _, state = run_turn(decision_final())
    assert f"┃ {QUESTION}" in log.stripped() and state.awaiting


def test_a_waiting_turn_with_an_options_question_keeps_every_line_of_it():
    final = OrchestratorResult(kind="confirm", question="Which sheet?\n\nOptions:\n- Draft\n- Final", decision_id="d")
    log, _, _ = run_turn(final)
    assert [ln for ln in log.stripped() if ln.startswith("┃")] == ["┃ Which sheet?", "┃", "┃ Options:", "┃ - Draft", "┃ - Final"]


def test_a_decision_event_mid_run_writes_nothing_until_the_turn_ends():
    async def go():
        app = QuestAITerminal(Session())
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = Log()
            app._tlog = log
            app._handle_event({"type": EVENT_DECISION, "text": QUESTION, "decision_id": "dec_1"})
            await pilot.pause()
            return log.lines, app.pending_question, app._activity._text

    lines, pending, status = asyncio.run(go())
    assert lines == [] and pending == QUESTION and status == "Waiting for your answer…"


# --- a heartbeat is a sign of life, never part of the record --------------------------------------

def test_a_heartbeat_event_shows_live_and_never_reaches_the_record():
    async def go():
        app = QuestAITerminal(Session())
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = Log()
            app._tlog = log
            app._console = Console(log)
            ev = {"run_id": "r1", "goal": GOAL}
            app._handle_event({"type": EVENT_EXEC, "text": "Read: /a.md", "data": ev})
            app._handle_event({"type": EVENT_EXEC, "text": "Deep run active, 90s elapsed, 12 actions so far",
                               "data": {"run_id": "r1", "phase": "heartbeat", "elapsed_seconds": 90.0}})
            app._handle_event({"type": EVENT_EXEC, "text": "Wrote 14 rows.", "data": ev})
            live = app._deep.get_dashboard()
            app._flush_pending_deep_runs()
            return live, "\n".join(log.lines), app._deep_archive["r1"]["exec_lines"]

    live, record, archived = asyncio.run(go())
    assert "Deep run active" in live
    assert "Deep run active" not in record and "Deep run active" not in "".join(archived)
    assert "Wrote 14 rows." in record


# --- the milestone echo and the repeated goal --------------------------------------------------------

def test_a_milestone_is_not_echoed_as_a_green_tick_before_the_work():
    async def go():
        app = QuestAITerminal(Session())
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = Log()
            app._tlog = log
            for text, data in ((f"Completed: {GOAL}", {"goal": GOAL, "run_id": "r1", "deep_output": "Done."}),
                               ("Worker output:\nHalf of it.", {"attempt": 1}),
                               ("Clarification needed: which sheet?", {})):
                app._handle_event({"type": EVENT_MILESTONE, "text": text, "data": data})
            return log.lines

    assert asyncio.run(go()) == []


# --- a failed model call --------------------------------------------------------------------------

def test_an_overloaded_provider_reads_in_plain_words_with_the_way_forward():
    err = RuntimeError("529 overloaded_error: The AI provider is temporarily overloaded. Please try again shortly.")
    assert outcome.provider_error_lines(err, message_restored=True) == [
        "The AI service is busy right now, so I couldn't answer.",
        "Your message is back in the box: press Enter to try again in a minute."]
    assert outcome.provider_error_lines(err, message_restored=False)[1] == "Send your message again to try again in a minute."


def test_a_rate_limit_is_busy_not_a_model_switch_suggestion():
    lines = outcome.provider_error_lines(RuntimeError("429 quota exceeded for this project"), message_restored=True)
    assert lines[0].startswith("The AI service is busy") and "switch" not in " ".join(lines).lower()


def test_a_usage_limit_error_names_the_limit_and_when_it_resets(utc):
    err = usage_limit.UsageLimitError(usage_limit.detect_usage_limit(LIMIT_REFUSAL, NOW))
    lines = outcome.provider_error_lines(err, message_restored=True, now=NOW)
    assert lines[0] == "Claude Code's usage limit is reached, so I couldn't answer. It resets at 20:00 UTC."
    assert lines[1] == "Your message is back in the box: press Enter to try again after it resets."


def test_an_unknown_error_keeps_its_first_words_and_no_code_dump():
    lines = outcome.provider_error_lines(ValueError("the key was rejected\n  File x line 3\n  more"), message_restored=False)
    assert lines[0].startswith("I couldn't answer: the key was rejected") and "\n" not in lines[0]


def test_a_failed_turn_puts_the_message_back_and_says_so():
    err = RuntimeError("529 overloaded_error: busy")

    def before(app):
        app.retry_text = "add the rows"

    log, begun, state = run_turn(None, error=err, before=before)
    assert state.prompt_text == "add the rows"
    assert log.stripped()[-6:-3] == ["Tester (AI):", "The AI service is busy right now, so I couldn't answer.",
                                     "Your message is back in the box: press Enter to try again in a minute."]
    assert not any("Error" in ln for ln in log.lines)
    assert begun == []


def test_a_failed_turn_leaves_text_the_person_already_typed_alone():
    err = RuntimeError("529 overloaded_error: busy")

    def before(app):
        app.retry_text = "add the rows"
        app.query_one("#prompt").text = "something else I was typing"

    log, _, state = run_turn(None, error=err, before=before)
    assert state.prompt_text == "something else I was typing"
    assert "Send your message again to try again in a minute." in log.stripped()


# --- who is speaking ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name, label", [("Quest AI", "Quest AI:"), ("My AI", "My AI:"), ("Zee", "Zee (AI):"),
                                         ("Assistant", "Assistant (AI):"), ("", "Assistant (AI):")])
def test_the_speaker_label_adds_the_ai_marker_only_when_the_name_lacks_one(name, label):
    assert outcome.speaker_label(name) == label


# --- no em dash on any screen ---------------------------------------------------------------------------

EM = "—"
EN = "–"


def string_literals(path: Path, skip_docstrings: bool = True):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    if skip_docstrings:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_string_the_terminal_can_print_contains_a_long_dash():
    files = sorted(glob.glob(str(REPO / "quest_ai_runner" / "textual_ui*.py"))) + [
        str(REPO / "quest_ai_runner" / "textual_session.py"),
        str(REPO / "quest_ai_runner" / "interactive_session_commands.py"),
        str(REPO / "quest_ai_runner" / "interactive_session_text.py")]
    found = [f"{Path(f).name}:{line}: {text[:60]!r}" for f in files for line, text in string_literals(Path(f))
             if EM in text or EN in text]
    assert not found, found


STATUS_CALLS = {"status", "ProgressEvent"}


def test_no_status_the_core_emits_contains_a_long_dash():
    found = []
    for f in sorted(glob.glob(str(REPO / "quest_ai_runner" / "core" / "*.py"))):
        tree = ast.parse(Path(f).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in STATUS_CALLS:
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and (EM in sub.value or EN in sub.value):
                            found.append(f"{Path(f).name}:{sub.lineno}: {sub.value[:60]!r}")
    assert not found, found


def test_what_the_new_outcome_module_writes_has_no_long_dash_either(utc):
    for text in [*outcome.launch_failure_lines(deep_final(STARTFAIL)),
                 *outcome.provider_error_lines(RuntimeError("529 overloaded"), message_restored=True),
                 *(outcome.closing_text(o, "at 15:40 UTC")[0] for o in (
                     outcome.OUTCOME_FINISHED, outcome.OUTCOME_STOPPED, outcome.OUTCOME_NOT_STARTED,
                     outcome.OUTCOME_NOT_RUN, outcome.OUTCOME_LIMIT, outcome.OUTCOME_WAITING)),
                 usage_limit.pause_status(usage_limit.detect_usage_limit(LIMIT_REFUSAL, NOW), NOW),
                 usage_limit.pause_status(None, NOW)]:
        assert EM not in text and EN not in text, text
        assert text[:1].isupper() or text[:1] in "✓~✗↻", text


def test_a_structured_launch_problem_is_worded_plainly_and_any_other_failure_keeps_the_runners_words():
    assert outcome.launch_failure_cause("The deep worker could not start: permission denied (x).",
                                        LAUNCH_PERMISSION_DENIED).endswith("because permission was denied.")
    other = DeepResult(met=False, launch_failed=True,
                       error="The deep worker could not start: There's an issue with the selected model (m).")
    assert outcome.launch_failure_lines(deep_final(other))[1] == (
        "The reason: There's an issue with the selected model (m).")


def test_the_runners_set_the_structured_launch_problem_from_the_process_outcome(tmp_path):
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
    missing = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=str(tmp_path / "no-such")))
    assert missing.run_goal(goal="g", brief="g").launch_problem == LAUNCH_MISSING_PROGRAM
    not_executable = tmp_path / "claude"
    not_executable.write_text("#!/bin/sh\n")
    denied = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=str(not_executable)))
    assert denied.run_goal(goal="g", brief="g").launch_problem == LAUNCH_PERMISSION_DENIED
    # The live-session runner reports the same two causes the same way.
    from quest_ai_runner.core.goal_runner_stream import StreamJsonGoalRunner
    stream_missing = StreamJsonGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=str(tmp_path / "no-such")))
    assert stream_missing.run_goal(goal="g", brief="g").launch_problem == LAUNCH_MISSING_PROGRAM
    stream_denied = StreamJsonGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=str(not_executable)))
    assert stream_denied.run_goal(goal="g", brief="g").launch_problem == LAUNCH_PERMISSION_DENIED


def test_the_prompt_waiting_for_an_answer_is_marked_and_the_mark_goes_when_it_is_answered():
    async def go():
        app = QuestAITerminal(Session())
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = Log()
            app._tlog = log
            app._console = Console(log)
            app._begin_turn = lambda *a, **k: None
            app._turn_active = True
            app._finish_turn("q", decision_final(), 5.0, cancelled=False, error=None)
            await pilot.pause()
            prompt = app.query_one("#prompt")
            marked = prompt.has_class("-awaiting")
            prompt.text = "skip them"
            await pilot.press("enter")
            await pilot.pause()
            return marked, prompt.has_class("-awaiting"), app._awaiting_decision

    assert asyncio.run(go()) == (True, False, False)
