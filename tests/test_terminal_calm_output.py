"""The terminal chat stays calm: diagnostics, the context block and long waits.

Three things used to bury a conversation or leave it looking frozen:

1. Every WARNING was copied into the transcript in full (time, level, logger, the whole message,
   wrapped over several lines), a library's included. Now the full record goes to a log file and the
   transcript gets at most one short dim line per distinct problem from this package.
2. The ``context`` event wrote a per-card dump (internal id, score, five file paths each). Now it is
   the count line, three titles and "and K more"; the detail is in the live context panel.
3. A deep run takes minutes to start and the status line said nothing. Now it says "Preparing the
   deep run" and counts the seconds once a wait passes three.

No terminal, no network: records are built by hand and fed to the handler, and the app is driven
through Textual's headless ``run_test``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quest_ai_runner.chat_conversations import chat_log_path
from quest_ai_runner.core.adapters import EVENT_CONTEXT, EVENT_EXEC, EVENT_PLAN, EVENT_STATUS
from quest_ai_runner.textual_ui import ActivityBar, QuestAITerminal, _RichLogHandler
from quest_ai_runner import textual_ui_widgets
from quest_ai_runner.core.adapters import EVENT_MILESTONE
from quest_ai_runner.core.metric_results import NOT_RECORDED_LINE
from quest_ai_runner.textual_ui_helpers import METRIC_LINES_SHOWN, context_block_lines, ellipsize, metric_lines
from quest_ai_runner.interactive_session_tracker import format_elapsed
from quest_ai_runner.textual_ui_widgets import (
    LOG_LINE_CHARS,
    LOG_LINES_SHOWN,
    short_log_text,
    with_elapsed,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

LONG_WARNING = (
    "Context assembly timed out after 30.0s with no partial result; turn-start context was dropped. "
    "Raise the assembly timeout if this recurs, and check which retrieval source is slow."
)


class RecordingLog:
    """Captures everything written to the transcript as plain text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(getattr(x, "plain", str(x)))


def make_record(name: str, message: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(name, level, "module.py", 1, message, None, None)


def make_handler(tmp_path: Path, verbosity: int = 0, log_path: Path | None = None):
    log = RecordingLog()
    path = log_path if log_path is not None else tmp_path / "chat.log"
    handler = _RichLogHandler(SimpleNamespace(_ui_thread_id=None), log, verbosity=verbosity, log_path=path)
    return handler, log, path


def run_app(build):
    """Build a ``QuestAITerminal`` with startup skipped, run ``build(app, pilot)`` once mounted."""
    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None  # skip the real (network) session build
        async with app.run_test() as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


# --- the log handler: a file for the record, one short line for the transcript ---------------

def test_the_transcript_gets_one_short_deduped_line_and_the_file_gets_every_full_record(tmp_path):
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record("quest-ai-runner.orchestrator", LONG_WARNING))
    handler.handle(make_record("quest-ai-runner.orchestrator", LONG_WARNING))
    handler.handle(make_record("quest_ai_runner.adapters.some_adapter", "A second, different problem. More."))
    handler.close()

    assert log.lines[0] == (
        "  ⚠ Context assembly timed out after 30.0s with no partial result; turn-start context was dropped"
    )
    shown = [ln for ln in log.lines if ln.startswith("  ⚠")]
    assert len(shown) == 2, "the repeated message is shown once"
    assert shown[1] == "  ⚠ A second, different problem"
    # No timestamp, level or logger name in anything the transcript shows.
    for line in log.lines:
        assert not re.match(r"\s*\d{4}-\d\d-\d\d", line)
        assert "WARNING" not in line and "quest-ai-runner.orchestrator" not in line
    assert all(len(ln) <= LOG_LINE_CHARS + 4 for ln in shown)

    text = path.read_text(encoding="utf-8")
    assert text.count(LONG_WARNING) == 2, "the file holds the whole record, every time it was logged"
    assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ WARNING quest-ai-runner.orchestrator ", text, re.M)
    assert "A second, different problem. More." in text


def test_the_details_hint_comes_once_with_the_log_path(tmp_path):
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record("quest_ai_runner.a", "First problem."))
    handler.handle(make_record("quest_ai_runner.b", "Second problem."))
    handler.handle(make_record("quest_ai_runner.c", "Third problem."))
    hints = [ln for ln in log.lines if "Details:" in ln]
    assert hints == [f"  Details: {path}, or run with -v"]
    assert log.lines.index(hints[0]) == 1, "the hint follows the first warning"
    handler.close()


def test_a_librarys_records_reach_the_file_but_never_the_transcript(tmp_path):
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record(
        "huggingface_hub.utils._http",
        "Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN.",
    ))
    handler.handle(make_record("httpx", "HTTP Request: GET https://example.org/", level=logging.ERROR))
    handler.close()
    assert log.lines == []
    text = path.read_text(encoding="utf-8")
    assert "unauthenticated requests" in text and "HTTP Request" in text


def test_verbose_mode_keeps_the_full_record_in_the_transcript_for_any_logger(tmp_path):
    handler, log, _path = make_handler(tmp_path, verbosity=1)
    handler.handle(make_record("huggingface_hub.utils._http", "Some library message. With a second sentence."))
    handler.close()
    assert len(log.lines) == 1
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ WARNING huggingface_hub.utils._http "
                    r"Some library message. With a second sentence.", log.lines[0])


def test_a_warning_is_cut_to_the_terminal_width_on_a_clause_or_a_word(tmp_path):
    log = RecordingLog()
    app = SimpleNamespace(_ui_thread_id=None, size=SimpleNamespace(width=80))
    handler = _RichLogHandler(app, log, verbosity=0, log_path=tmp_path / "chat.log")
    handler.handle(make_record(
        "quest-ai-runner.orchestrator",
        "Context assembly timed out after 30.0s with no partial result; turn-start context was dropped "
        "and this turn proceeds without it (QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust)"))
    handler.handle(make_record(
        "quest_ai_runner.adapters.vector_context_assembler",
        "Card store has 142781 cards; only the first 1000 are searched by meaning "
        "(prune it with quarantine_cards.py)."))
    handler.close()
    shown = [ln for ln in log.lines if ln.startswith("  ⚠")]
    assert shown[0] == "  ⚠ Context assembly timed out after 30.0s with no partial result…"
    assert shown[1] == "  ⚠ Card store has 142781 cards; only the first 1000 are searched by meaning", (
        "a trailing aside in parentheses is the first thing to go")
    assert all(len(ln) <= 76 for ln in shown), "no warning line wraps in an 80-column terminal"


def test_a_long_first_sentence_is_ellipsized_and_a_multiline_message_uses_its_first_line():
    text = short_log_text("x" * 300)
    assert len(text) == LOG_LINE_CHARS and text.endswith("…")
    assert short_log_text("Failed to open the store.\nTraceback (most recent call last):\n  File ...") == (
        "Failed to open the store"
    )
    assert short_log_text("Took 30.0s to finish and then failed") == "Took 30.0s to finish and then failed"
    assert short_log_text("   ") == ""


def test_after_a_handful_of_distinct_warnings_the_rest_go_to_the_file_only(tmp_path):
    handler, log, path = make_handler(tmp_path)
    for n in range(LOG_LINES_SHOWN + 5):
        handler.handle(make_record("quest_ai_runner.noisy", f"Problem number {n} happened."))
    handler.close()
    problems = [ln for ln in log.lines if ln.startswith("  ⚠ Problem")]
    assert len(problems) == LOG_LINES_SHOWN
    assert [ln for ln in log.lines if "More warnings" in ln] == ["  ⚠ More warnings are in the log file only"]
    assert path.read_text(encoding="utf-8").count("happened.") == LOG_LINES_SHOWN + 5


def test_an_unwritable_log_location_never_raises_and_the_hint_falls_back_to_verbose(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    handler, log, _path = make_handler(tmp_path, log_path=blocker / "chat.log")
    handler.handle(make_record("quest_ai_runner.a", "Something degraded."))
    handler.close()
    assert log.lines == ["  ⚠ Something degraded", "  Details: run with -v"]


def test_the_log_file_is_rotated_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(textual_ui_widgets, "LOG_FILE_BYTES", 400)
    handler, _log, path = make_handler(tmp_path)
    for n in range(200):
        handler.handle(make_record("quest_ai_runner.a", f"Record {n}: " + "x" * 40))
    handler.close()
    files = sorted(tmp_path.glob("chat.log*"))
    assert 1 < len(files) <= textual_ui_widgets.LOG_FILE_BACKUPS + 1
    assert all(f.stat().st_size < 400 + 200 for f in files)


def test_the_log_path_sits_beside_the_conversations_and_an_override_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("QAR_CHAT_LOG_FILE", raising=False)
    monkeypatch.setenv("QAR_CHAT_HISTORY_DIR", str(tmp_path / "state" / "conversations"))
    assert chat_log_path() == tmp_path / "state" / "logs" / "chat.log"
    monkeypatch.setenv("QAR_CHAT_LOG_FILE", str(tmp_path / "mine.log"))
    assert chat_log_path() == tmp_path / "mine.log"


def test_in_the_running_app_a_worker_thread_warning_is_one_line_and_a_library_warning_is_silent(tmp_path):
    async def build(app, pilot):
        rec = RecordingLog()
        app.log_handler._tlog = rec
        await asyncio.to_thread(logging.getLogger("quest_ai_runner.some_adapter").warning,
                                "Something degraded. Long detail follows here.")
        await asyncio.to_thread(logging.getLogger("huggingface_hub.utils._http").warning,
                                "You are sending unauthenticated requests to the HF Hub.")
        await pilot.pause()
        return rec.lines, app.log_handler

    lines, handler = run_app(build)
    assert lines[0] == "  ⚠ Something degraded"
    assert not any("unauthenticated" in ln for ln in lines)
    assert handler not in logging.getLogger().handlers, "the handler is removed when the app closes"
    assert "unauthenticated requests" in Path(os.environ["QAR_CHAT_LOG_FILE"]).read_text(encoding="utf-8")


# --- the Hugging Face warning is stopped at the source ---------------------------------------

def child_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_VERBOSITY"}
    env.update(extra)
    return env


def test_importing_the_cli_sets_the_hub_verbosity_default_and_a_users_value_wins():
    code = "import os, quest_ai_runner.cli; print(os.environ.get('HF_HUB_VERBOSITY'))"
    default = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=child_env(),
                             capture_output=True, text=True, timeout=120)
    assert default.stdout.strip() == "error", default.stderr[-500:]
    own = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                         env=child_env(HF_HUB_VERBOSITY="info"), capture_output=True, text=True, timeout=120)
    assert own.stdout.strip() == "info", own.stderr[-500:]


def test_the_hub_logger_stays_at_error_even_though_its_import_resets_the_level():
    pytest.importorskip("huggingface_hub")
    # Importing the hub's utilities is what configures its logger (it is lazy about the rest).
    code = ("import logging, quest_ai_runner.cli, huggingface_hub.utils; "
            "print(logging.getLogger('huggingface_hub').level)")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=child_env(),
                            capture_output=True, text=True, timeout=120)
    assert result.stdout.strip() == str(logging.ERROR), result.stderr[-500:]


# --- the context event is one calm block ------------------------------------------------------

def many_cards(n: int = 6) -> list[dict]:
    return [
        {
            "id": f"card:area-aaaaaa--root-bbbbbb--topic-{i}-cccccc",
            "title": f"Topic {i} " + "with a rather long descriptive title " * 4,
            "adapter": "files",
            "relevance_score": 0.5 + i / 20,
            "file_count": 5,
            "files": [f"/srv/corpus/area/nested/folder/file_{i}_{j}.md" for j in range(5)],
        }
        for i in range(n)
    ]


SOURCES = [{"label": "Corpus files", "adapter": "files", "items": ["/srv/corpus/a.md", "/srv/corpus/b.md"]},
           {"label": "Recent turns", "adapter": "recent", "items": []}]


def test_the_context_block_is_the_count_three_titles_and_how_many_more():
    lines = [ln.plain for ln in context_block_lines(many_cards(), SOURCES, width=80)]
    assert lines[0] == "Context: 6 cards, 2 sources"
    bullets = [ln for ln in lines if ln.startswith("  ●")]
    assert len(bullets) == 3
    assert bullets[0].startswith("  ● Topic 5 "), "the highest score comes first"
    assert "  … and 3 more" in lines
    assert lines[-1] == "  Sources: Corpus files (2)", "labels and counts only, no item lists"
    assert len(lines) <= 6
    blob = "\n".join(lines)
    for hidden in ("card:", "area-aaaaaa", "score", "/srv/", ".md", "files"):
        assert hidden not in blob.replace("Corpus files", "")


@pytest.mark.parametrize("width", [20, 30, 60, 100])
def test_no_context_line_is_wider_than_the_terminal(width):
    for line in context_block_lines(many_cards(), SOURCES, width=width):
        assert len(line.plain) <= max(width, 14), (width, line.plain)


def test_a_card_without_a_title_never_falls_back_to_its_internal_id():
    lines = [ln.plain for ln in context_block_lines([{"id": "card:area-aaaaaa", "relevance_score": 0.9}], [])]
    assert lines == ["Context: 1 card, 0 sources", "  ● Untitled card"]


def test_ellipsize_cuts_to_the_limit_on_one_line():
    assert ellipsize("short", 70) == "short"
    cut = ellipsize("word " * 40, 30)
    assert len(cut) <= 30 and cut.endswith("…") and "\n" not in cut
    assert ellipsize("two\nlines here", 70) == "two lines here"


def test_the_context_event_writes_the_calm_block_and_still_feeds_the_live_panel():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        cards = many_cards()
        app._handle_event({"type": EVENT_CONTEXT, "text": "",
                           "data": {"card_metadata": cards, "sources": SOURCES}})
        await pilot.pause()
        return rec.lines, app._ctx.cards, cards

    lines, panel_cards, cards = run_app(build)
    assert len(lines) <= 6
    assert lines[0] == "Context: 6 cards, 2 sources"
    assert not any("card:" in ln or "/srv/" in ln or "score" in ln for ln in lines)
    assert panel_cards == cards, "the in-place panel still receives every card unchanged"


# --- the metric before and after: dim, short, one line each -------------------------------------

def metric_entry(i: int = 0, **extra) -> dict:
    return {"solution_id": "greet-a-person", "metric_id": f"metric-{i}", "source": "measured", "reason": "",
            "name": f"Share of sample names greeted in the standard form, variant {i}", "milestone": "",
            "before": 0.5, "after": 0.75, "delta": 0.25, "disagrees": False, **extra}


@pytest.mark.parametrize("width", [20, 30, 60, 100])
def test_no_metric_line_is_wider_than_the_terminal(width):
    entries = [metric_entry(0), metric_entry(1, source="recorded", disagrees=True),
               {"source": "not_applicable", "reason": "It only edits a draft, which no metric covers at all.",
                "name": "", "metric_id": "", "before": None, "after": None}]
    for line in metric_lines(entries, True, width=width):
        assert len(line.plain) <= max(width, 14), (width, line.plain)
        assert line.style == "dim"


def test_a_long_metric_name_gives_way_before_the_numbers_and_the_source_do():
    (line,) = metric_lines([metric_entry(7)], width=60)
    assert line.plain.endswith(": 0.5 -> 0.75 (measured by QAR)") and "…" in line.plain
    assert len(line.plain) <= 60


def test_a_metric_block_is_a_few_lines_and_how_many_more():
    lines = [ln.plain for ln in metric_lines([metric_entry(i, name=f"Metric {i}") for i in range(7)], True, width=80)]
    assert len(lines) == METRIC_LINES_SHOWN + 2
    assert lines[0] == "  Metric 0: 0.5 -> 0.75 (measured by QAR)"
    assert lines[METRIC_LINES_SHOWN] == f"  … and {7 - METRIC_LINES_SHOWN} more"
    assert lines[-1] == f"  {NOT_RECORDED_LINE}"
    assert metric_lines([], False) == [], "nothing to say, nothing written"


def test_the_metric_wording_uses_plain_words_an_arrow_made_of_ascii_and_no_em_dash():
    lines = [ln.plain for ln in metric_lines([
        metric_entry(0, name="Pass rate"), metric_entry(1, name="Latency", source="recorded", disagrees=True),
        {"source": "not_applicable", "reason": "Only a draft.", "name": "", "metric_id": ""}], True)]
    assert lines == ["  Pass rate: 0.5 -> 0.75 (measured by QAR)",
                     "  Latency: 0.5 -> 0.75 (recorded by the AI, disagrees with QAR)",
                     "  Metric not applicable: Only a draft.",
                     f"  {NOT_RECORDED_LINE}"]
    assert not any("—" in ln for ln in lines)


def test_in_the_running_app_a_finished_goals_metric_is_one_transcript_line():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        app._deep.add_run("r1", "Improve the greeting")
        app._deep.set_final_output("r1", "Changed greet().")
        app._handle_event({"type": EVENT_MILESTONE, "text": "Completed: Improve the greeting",
                           "data": {"run_id": "r1", "metrics": [metric_entry(0, name="Pass rate")]}})
        app._flush_deep_run("r1")
        await pilot.pause()
        return rec.lines

    lines = run_app(build)
    assert lines.count("  Pass rate: 0.5 -> 0.75 (measured by QAR)") == 1


# --- a long wait counts its seconds ------------------------------------------------------------

def test_format_elapsed_and_the_status_text():
    assert format_elapsed(45) == "45s"
    assert format_elapsed(125) == "2m5s"
    assert with_elapsed("Preparing the deep run…", 45) == "Preparing the deep run… 45s"
    assert with_elapsed("Gathering context", 7) == "Gathering context  · 7s"
    assert with_elapsed("Thinking…", None) == "Thinking…"


def test_the_clock_shows_after_three_seconds_survives_status_changes_and_stops():
    now = [100.0]
    bar = ActivityBar()
    bar.clock = lambda: now[0]
    bar.set_status("preparing the deep run…")
    assert bar.render().plain.endswith("Preparing the deep run…"), "status text starts uppercase"
    bar.start_clock()
    now[0] = 102.9
    assert bar.render().plain.endswith("Preparing the deep run…"), "no counter for a short wait"
    now[0] = 103.2
    assert bar.render().plain.endswith("Preparing the deep run… 3s")
    now[0] = 145.0
    bar.set_status("Starting the worker…")
    assert bar.render().plain.endswith("Starting the worker… 45s"), "a status change does not reset it"
    bar.stop_clock()
    assert bar.render().plain.endswith("Starting the worker…")
    now[0] = 400.0
    assert bar.elapsed_seconds() is None


def test_a_deep_plan_shows_the_preparing_status_with_a_clock_that_the_first_exec_event_clears():
    now = [1000.0]

    async def build(app, pilot):
        app._run_stream = lambda user_text: None
        app._activity.clock = lambda: now[0]
        app._begin_turn("please do the work", echo=True)
        app._handle_event({"type": EVENT_PLAN, "action": "deep", "text": "internal routing rationale"})
        shown = {}
        now[0] = 1045.0
        shown["preparing"] = app._activity.render().plain
        app._handle_event({"type": EVENT_STATUS, "text": "Gathering the context for the run…"})
        now[0] = 1046.0
        shown["status"] = app._activity.render().plain
        app._handle_event({"type": EVENT_EXEC, "text": "Reading files",
                           "data": {"run_id": "r1", "goal": "do the work"}})
        now[0] = 1100.0
        shown["exec"] = app._activity.render().plain
        return shown

    shown = run_app(build)
    assert shown["preparing"].endswith("Preparing the deep run… 45s")
    assert shown["status"].endswith("Gathering the context for the run… 46s"), "a real status replaces the text"
    assert shown["exec"].endswith("Executing…"), shown["exec"]


def test_the_clock_resets_per_turn_and_stops_when_the_turn_finishes():
    now = [50.0]

    async def build(app, pilot):
        app._run_stream = lambda user_text: None
        app._activity.clock = lambda: now[0]
        app._begin_turn("first", echo=True)
        started_first = app._activity.wait_started
        now[0] = 80.0
        app._activity.stop_clock()
        app._turn_active = False
        app._begin_turn("second", echo=True)
        return started_first, app._activity.wait_started

    first, second = run_app(build)
    assert (first, second) == (50.0, 80.0)
