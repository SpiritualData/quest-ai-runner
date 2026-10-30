"""The terminal chat stays calm: diagnostics, the context block and long waits.

Three things used to bury a conversation or leave it looking frozen:

1. Every WARNING was copied into the transcript in full (time, level, logger, the whole message,
   wrapped over several lines), a library's included. Now the full record goes to a log file and the
   transcript gets at most one short dim line per distinct problem from this package.
2. The ``context`` event wrote a per-card dump (internal id, score, five file paths each). Now it is
   one sentence, ``Found 6 sources: A, B and 4 more.``; the full list is behind Alt+C.
3. A long job takes minutes to start and the status line said nothing. Now it says "Getting ready to
   start the work" and counts the time once a wait passes three, ahead of the text so a narrow
   terminal never cuts the clock.

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
from quest_ai_runner.core.adapters_events import EVENT_CONTEXT, EVENT_EXEC, EVENT_PLAN, EVENT_STATUS
from quest_ai_runner.textual_ui import ActivityBar, QuestAITerminal, _RichLogHandler
from quest_ai_runner import textual_ui_widgets
from quest_ai_runner.core.adapters_events import EVENT_MILESTONE
from quest_ai_runner.core.metric_results import NOT_RECORDED_LINE
from quest_ai_runner.textual_ui_helpers import (
    METRIC_LINES_SHOWN, context_block_lines, ellipsize, metric_lines, metric_number, union_entries,
)
from quest_ai_runner.interactive_session_tracker import format_elapsed
from quest_ai_runner.textual_ui_widgets import (
    LOG_LINE_CHARS,
    LOG_LINES_SHOWN,
    short_log_text,
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


def test_the_details_hint_is_the_last_line_of_the_group_never_between_two_warnings(tmp_path):
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record("quest_ai_runner.a", "First problem."))
    handler.handle(make_record("quest_ai_runner.b", "Second problem."))
    handler.handle(make_record("quest_ai_runner.c", "Third problem."))
    assert log.lines == ["  ⚠ First problem", "  ⚠ Second problem", "  ⚠ Third problem"], "no hint yet"
    handler.write_summary()
    handler.write_summary()   # once
    assert log.lines[-2:] == [f"  Details: {path}", ""], "a blank line after it, so the next line is not run into it"
    assert [ln for ln in log.lines if "Details:" in ln] == [log.lines[-2]]
    handler.close()


def test_a_second_burst_in_the_same_turn_is_shown_while_the_turn_has_room_and_each_burst_is_closed_once(tmp_path):
    """The closing line used to switch every later warning off for the rest of the session."""
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record("quest_ai_runner.a", "First problem."))
    handler.write_summary()
    handler.handle(make_record("quest_ai_runner.b", "A later problem."))
    handler.write_summary()
    handler.close()
    assert [ln for ln in log.lines if ln] == ["  ⚠ First problem", f"  Details: {path}",
                                              "  ⚠ A later problem", f"  Details: {path}"]


def test_a_new_turn_starts_its_own_three_lines_and_its_own_count(tmp_path):
    handler, log, path = make_handler(tmp_path)
    for n in range(5):
        handler.handle(make_record("quest_ai_runner.noisy", f"First turn problem {n}."))
    handler.begin_turn()                      # the person sends their next message
    for n in range(5):
        handler.handle(make_record("quest_ai_runner.noisy", f"Second turn problem {n}."))
    handler.handle(make_record("quest_ai_runner.noisy", "First turn problem 0."))   # new again in a new turn
    handler.write_summary()
    handler.close()
    text = [ln for ln in log.lines if ln]
    assert [ln for ln in text if "First turn" in ln] == [
        "  ⚠ First turn problem 0", "  ⚠ First turn problem 1", "  ⚠ First turn problem 2"]
    assert [ln for ln in text if "Second turn" in ln] == [
        "  ⚠ Second turn problem 0", "  ⚠ Second turn problem 1", "  ⚠ Second turn problem 2"]
    assert [ln for ln in text if "more notices" in ln] == [
        f"  2 more notices. Details: {path}", f"  3 more notices. Details: {path}"], \
        "each burst says how many it did not show, once, after the lines it closes"
    summaries = [i for i, ln in enumerate(text) if "Details:" in ln]
    problems = [i for i, ln in enumerate(text) if "⚠" in ln]
    assert all(not (problems[0] < s < problems[-1]) or s > problems[2] for s in summaries[:1])
    assert text.index(f"  2 more notices. Details: {path}") == 3, "right after the third line, never between"


def test_an_error_is_always_shown_and_never_counted_away(tmp_path):
    handler, log, path = make_handler(tmp_path)
    for n in range(LOG_LINES_SHOWN + 2):
        handler.handle(make_record("quest_ai_runner.noisy", f"Warning {n}."))
    handler.handle(make_record("quest_ai_runner.files", "Could not read the notes folder.", level=logging.ERROR))
    handler.handle(make_record("quest_ai_runner.files", "Could not read the notes folder.", level=logging.ERROR))
    handler.handle(make_record("quest_ai_runner.files", "Another error.", level=logging.CRITICAL))
    handler.write_summary()
    handler.close()
    text = [ln for ln in log.lines if ln]
    assert "  ⚠ Could not read the notes folder" in text and "  ⚠ Another error" in text
    assert text.count("  ⚠ Could not read the notes folder") == 1, "the same error is shown once a turn"
    assert text[-1] == f"  2 more notices. Details: {path}", "errors are not among the notices counted away"


def test_a_record_that_says_it_is_not_for_the_transcript_goes_to_the_file_only(tmp_path):
    handler, log, path = make_handler(tmp_path)
    record = make_record("quest_ai_runner.core.usage_limit", "Claude Code usage limit reached (weekly limit); resets at 20:00")
    record.in_transcript = False
    handler.handle(record)
    handler.write_summary()
    handler.close()
    assert log.lines == []
    assert "usage limit reached" in path.read_text(encoding="utf-8")


def test_the_usage_limit_record_is_marked_for_the_file_only_so_the_closing_line_is_the_only_telling(tmp_path):
    from quest_ai_runner.core import usage_limit
    usage_limit.reset_for_tests()
    captured: list = []

    class Catch(logging.Handler):
        def emit(self, record):
            captured.append(record)

    logger = usage_limit.log
    catch = Catch(level=logging.WARNING)
    logger.addHandler(catch)
    try:
        usage_limit.record(usage_limit.detect_usage_limit("You've hit your weekly limit · resets 1pm (UTC)"))
    finally:
        logger.removeHandler(catch)
        usage_limit.reset_for_tests()
    assert [getattr(r, "in_transcript", True) for r in captured] == [False]


def test_nothing_is_written_to_close_a_group_that_never_had_a_warning(tmp_path):
    handler, log, _path = make_handler(tmp_path)
    handler.write_summary()
    handler.close()
    assert log.lines == []


def test_a_librarys_records_reach_the_file_but_never_the_transcript(tmp_path):
    handler, log, path = make_handler(tmp_path)
    handler.handle(make_record(
        "huggingface_hub.utils._http",
        "Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN.",
    ))
    handler.handle(make_record("httpx", "HTTP Request: GET https://example.org/"))
    handler.close()
    assert log.lines == []
    text = path.read_text(encoding="utf-8")
    assert "unauthenticated requests" in text and "HTTP Request" in text


def test_a_librarys_error_is_shown_because_an_error_is_never_chatter(tmp_path):
    handler, log, _path = make_handler(tmp_path)
    handler.handle(make_record("httpx", "Request failed for good: connection refused.", level=logging.ERROR))
    handler.close()
    assert log.lines == ["  ⚠ Request failed for good: connection refused"]


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


def test_after_three_distinct_warnings_the_rest_are_counted_in_one_closing_line(tmp_path):
    assert LOG_LINES_SHOWN == 3
    handler, log, path = make_handler(tmp_path)
    for n in range(LOG_LINES_SHOWN + 5):
        handler.handle(make_record("quest_ai_runner.noisy", f"Problem number {n} happened."))
    handler.handle(make_record("quest_ai_runner.noisy", "Problem number 4 happened."))   # a repeat counts once
    handler.write_summary()
    handler.close()
    problems = [ln for ln in log.lines if ln.startswith("  ⚠ Problem")]
    assert len(problems) == LOG_LINES_SHOWN
    assert log.lines[-2:] == [f"  5 more notices. Details: {path}", ""]
    assert len(log.lines) == LOG_LINES_SHOWN + 2
    assert path.read_text(encoding="utf-8").count("happened.") == LOG_LINES_SHOWN + 6


def test_an_unwritable_log_location_never_raises_and_the_hint_falls_back_to_verbose(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    handler, log, _path = make_handler(tmp_path, log_path=blocker / "chat.log")
    handler.handle(make_record("quest_ai_runner.a", "Something degraded."))
    handler.write_summary()
    handler.close()
    assert log.lines == ["  ⚠ Something degraded", "  Details: run with -v", ""]


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


def test_the_context_block_is_one_sentence_two_titles_and_how_many_more():
    lines = [ln.plain for ln in context_block_lines(many_cards(), SOURCES)]
    assert len(lines) == 1
    assert lines[0].startswith("  Looked at 6 sources: Topic 5 "), "the highest score comes first"
    assert lines[0].count("Topic ") == 2 and lines[0].endswith(" and 4 more.")
    blob = "\n".join(lines)
    for hidden in ("card", "area-aaaaaa", "score", "/srv/", ".md", "Corpus files", "Sources:"):
        assert hidden not in blob


def test_the_context_line_names_every_source_when_there_are_two_or_fewer():
    cards = [{"title": "Pricing tiers", "relevance_score": 0.9}, {"title": "Refund policy", "relevance_score": 0.5}]
    assert [ln.plain for ln in context_block_lines(cards, [])] == ["  Looked at 2 sources: Pricing tiers and Refund policy."]
    assert [ln.plain for ln in context_block_lines(cards[:1], [])] == ["  Looked at 1 source: Pricing tiers."]
    assert context_block_lines([], []) == [], "nothing found, nothing said"


def test_a_long_title_is_cut_and_the_sentence_wraps_instead_of_being_clipped():
    (line,) = context_block_lines(many_cards(), SOURCES)
    assert all(len(part) <= 51 for part in line.plain.split(": ", 1)[1].split(", ")[:1])
    assert "…" in line.plain


def test_a_source_without_a_title_never_falls_back_to_its_internal_id():
    lines = [ln.plain for ln in context_block_lines([{"id": "card:area-aaaaaa", "relevance_score": 0.9}], [])]
    assert lines == ["  Looked at 1 source: Untitled source."]


def test_with_no_cards_the_items_of_the_source_groups_are_the_count():
    lines = [ln.plain for ln in context_block_lines([], SOURCES)]
    assert lines == ["  Looked at 2 sources."]


def test_ellipsize_cuts_to_the_limit_on_one_line():
    assert ellipsize("short", 70) == "short"
    cut = ellipsize("word " * 40, 30)
    assert len(cut) <= 30 and cut.endswith("…") and "\n" not in cut
    assert ellipsize("two\nlines here", 70) == "two lines here"


def test_the_context_event_writes_the_sentence_and_still_feeds_the_turns_state():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        cards = many_cards()
        app._handle_event({"type": EVENT_CONTEXT, "text": "",
                           "data": {"card_metadata": cards, "sources": SOURCES}})
        await pilot.pause()
        return rec.lines, app._ctx.cards, cards

    lines, kept_cards, cards = run_app(build)
    assert len(lines) == 1 and lines[0].startswith("  Looked at 6 sources:")
    assert not any("card" in ln or "/srv/" in ln or "score" in ln for ln in lines)
    assert kept_cards == cards, "the turn keeps every card unchanged (behind Alt+C)"


# --- the metric before and after: one readable sentence each --------------------------------------

def metric_entry(i: int = 0, **extra) -> dict:
    return {"solution_id": "greet-a-person", "metric_id": f"metric-{i}", "source": "measured", "reason": "",
            "name": f"Share of sample names greeted in the standard form, variant {i}", "milestone": "",
            "before": 0.5, "after": 0.75, "delta": 0.25, "disagrees": False, **extra}


def test_a_metric_line_keeps_its_whole_name_and_wraps_instead_of_being_cut():
    (line,) = metric_lines([metric_entry(7)])
    assert line.plain == ("  Share of sample names greeted in the standard form, variant 7: 0.5 → 0.75 "
                          "(measured automatically)")
    assert "…" not in line.plain and line.style == "", "normal weight, not dim"


def test_two_metrics_with_similar_names_stay_distinguishable():
    first, second = [ln.plain for ln in metric_lines([metric_entry(1), metric_entry(2)])]
    assert "variant 1" in first and "variant 2" in second


@pytest.mark.parametrize("value, shown", [
    (0.38, "0.38"), (0.4, "0.4"), (0.380, "0.38"), (1.0, "1"), (0, "0"), (12, "12"), (0.12345, "0.123"),
    (1234.5678, "1234.568"), (0.0004, "0.0004"), (-0.5, "-0.5"), (None, "unknown"),
])
def test_metric_numbers_have_at_most_three_decimals_and_no_trailing_zeros_and_no_invented_unit(value, shown):
    assert metric_number(value) == shown
    assert "%" not in metric_number(value)


def test_a_measured_and_a_recorded_entry_that_agree_are_one_line():
    lines = [ln.plain for ln in metric_lines([
        metric_entry(0, name="Pass rate"), metric_entry(0, name="Pass rate", source="recorded")])]
    assert lines == ["  Pass rate: 0.5 → 0.75 (measured automatically)"]


def test_a_disagreement_is_stated_neutrally_in_one_line_in_normal_weight_and_only_the_note_has_the_warning_colour():
    lines = metric_lines([
        metric_entry(0, name="Pass rate", before=0.38, after=0.91, disagrees=True),
        metric_entry(0, name="Pass rate", source="recorded", before=0.4, after=0.9, disagrees=True)])
    assert [ln.plain for ln in lines] == [
        "  Pass rate: 0.38 → 0.91 (measured automatically). The AI reported 0.4 → 0.9."]
    assert lines[0].style == "" and "disagree" not in lines[0].plain and "QAR" not in lines[0].plain
    note = "The AI reported 0.4 → 0.9."
    start = lines[0].plain.index(note)
    assert [(span.start, span.end, str(span.style)) for span in lines[0].spans] == [(start, start + len(note), "yellow")], \
        "the warning colour is on the note, not on the whole line"


def test_a_number_the_measured_entry_lacks_is_not_left_unknown_when_the_ai_reported_it():
    """A metric the runner could read only before the work: the AI's after is stated beside it, not hidden behind "unknown"."""
    (line,) = metric_lines([metric_entry(0, name="Pass rate", before=1.0, after=None, delta=None),
                            metric_entry(0, name="Pass rate", source="recorded", before=1.0, after=0.9)])
    assert line.plain == "  Pass rate: 1 → unknown (measured automatically). The AI reported 1 → 0.9."
    (alone,) = metric_lines([metric_entry(0, name="Pass rate", before=1.0, after=None, delta=None)])
    assert alone.plain == "  Pass rate: 1 → unknown (measured automatically)", "nothing to add when nothing else was reported"


def test_the_same_results_seen_by_several_sources_are_one_block_and_a_repeat_within_one_source_is_kept():
    a, b = metric_entry(1, name="A"), metric_entry(2, name="B")
    assert union_entries([a, b], [a, b], [b]) == [a, b]
    assert union_entries([a, a], [a]) == [a, a], "two alike results of one source are two results"
    assert union_entries([a], [b, "not an entry"], None) == [a, b]
    assert union_entries() == []


def test_a_disagreement_whose_partner_is_not_in_the_batch_still_says_the_numbers_differ():
    (line,) = metric_lines([metric_entry(0, name="Pass rate", disagrees=True)])
    assert line.plain == "  Pass rate: 0.5 → 0.75 (measured automatically). The AI reported different numbers."
    (line,) = metric_lines([metric_entry(0, name="Pass rate", source="recorded", disagrees=True)])
    assert line.plain == "  Pass rate: 0.5 → 0.75 (reported by the AI). The measured numbers differ."


def test_the_source_is_said_in_plain_words_never_by_an_internal_name():
    lines = [ln.plain for ln in metric_lines([
        metric_entry(0, name="Pass rate"), metric_entry(1, name="Latency", source="recorded"),
        {"source": "not_applicable", "reason": "Only a draft.", "name": "", "metric_id": ""}], True)]
    assert lines == ["  Latency: 0.5 → 0.75 (reported by the AI)",
                     "  Pass rate: 0.5 → 0.75 (measured automatically)",
                     "  Nothing to measure. Only a draft.",
                     f"  {NOT_RECORDED_LINE}"], "by metric name, the declared not-applicable last"
    assert not any("QAR" in ln or "—" in ln for ln in lines)


def test_a_metric_block_is_a_few_lines_and_a_count_that_says_where_the_rest_are():
    entries = [metric_entry(i, name=f"Metric {i}") for i in range(7)]
    lines = [ln.plain for ln in metric_lines(entries, True, more_hint="Alt+D shows them all.")]
    assert len(lines) == METRIC_LINES_SHOWN + 2
    assert lines[0] == "  Metric 0: 0.5 → 0.75 (measured automatically)"
    assert lines[METRIC_LINES_SHOWN] == f"  {7 - METRIC_LINES_SHOWN} more metric results. Alt+D shows them all."
    assert lines[-1] == f"  {NOT_RECORDED_LINE}"
    assert metric_lines([], False) == [], "nothing to say, nothing written"
    assert len(metric_lines(entries, limit=None)) == 7, "with no limit nothing is folded away"


def test_in_the_running_app_a_finished_goals_metric_is_one_transcript_line():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        app._deep.add_run("r1", "Improve the greeting")
        app._deep.set_final_output("r1", "Changed greet().")
        app._handle_event({"type": EVENT_MILESTONE, "text": "Completed: Improve the greeting",
                           "data": {"run_id": "r1", "metrics": [metric_entry(0, name="Pass rate")]}})
        app._flush_deep_run("r1")
        app.write_turn_metrics()
        await pilot.pause()
        return rec.lines

    lines = run_app(build)
    assert lines.count("  Pass rate: 0.5 → 0.75 (measured automatically)") == 1


def test_the_metric_block_comes_after_the_result_and_its_link_and_before_the_closing_line():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        app._console.markdown = lambda text: rec.write(SimpleNamespace(plain="ANSWER " + text))
        app._deep.add_run("r1", "Improve the greeting")
        app._deep.update_run_output("r1", "Read: greet.py")
        app._deep.set_final_output("r1", "Changed greet(). Link: https://example.org/x")
        app._deep.set_metrics("r1", [metric_entry(0, name="Pass rate")])
        app._flush_deep_run("r1")
        app.write_turn_metrics()
        await pilot.pause()
        return rec.lines

    lines = run_app(build)
    metric_at = lines.index("  Pass rate: 0.5 → 0.75 (measured automatically)")
    answer_at = next(i for i, ln in enumerate(lines) if ln.startswith("ANSWER"))
    assert answer_at < metric_at, "the AI's answer and its link first, then the numbers"


def test_alt_d_lists_every_metric_result_the_transcript_folded_away():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        app._deep.add_run("r1", "Improve the greeting")
        app._deep.update_run_output("r1", "Read: greet.py")
        entries = [metric_entry(i, name=f"Metric {i}") for i in range(7)]
        app._deep.set_metrics("r1", entries)
        app._flush_deep_run("r1")
        app.write_turn_metrics()
        app.action_toggle_deep_detail()
        await pilot.pause()
        return rec.lines, str(app._deep_detail._body.render())

    lines, panel = run_app(build)
    assert "  3 more metric results. Alt+D shows them all." in lines
    for i in range(7):
        assert f"Metric {i}: 0.5 → 0.75 (measured automatically)" in panel


def test_with_no_job_to_open_nothing_is_folded_away_so_the_count_is_never_a_dead_end():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        app.write_metrics([metric_entry(i, name=f"Metric {i}") for i in range(7)])
        await pilot.pause()
        return rec.lines

    lines = run_app(build)
    assert len([ln for ln in lines if ln.startswith("  Metric ")]) == 7
    assert not any("more metric results" in ln for ln in lines)


# --- a long wait counts its seconds ------------------------------------------------------------

def test_format_elapsed_is_the_one_way_a_time_is_written():
    assert format_elapsed(45) == "45s"
    assert format_elapsed(125) == "2m5s"
    assert format_elapsed(212.4) == "3m32s"


def test_the_clock_shows_after_three_seconds_survives_status_changes_and_stops():
    now = [100.0]
    bar = ActivityBar()
    bar.clock = lambda: now[0]
    bar.set_status("preparing the work…")
    assert bar.render().plain.endswith("Preparing the work…"), "status text starts uppercase"
    assert "s  " not in bar.render().plain
    bar.start_clock()
    now[0] = 102.9
    assert bar.render().plain.endswith("●  Preparing the work…"), "no counter for a short wait"
    now[0] = 103.2
    assert bar.render().plain.endswith("●  3s  Preparing the work…"), "the clock stands before the text"
    now[0] = 145.0
    bar.set_status("Starting the work…")
    assert bar.render().plain.endswith("●  45s  Starting the work…"), "a status change does not reset it"
    bar.stop_clock()
    assert bar.render().plain.endswith("●  Starting the work…")
    now[0] = 400.0
    assert bar.elapsed_seconds() is None


@pytest.mark.parametrize("queued", [0, 2])
def test_at_80_columns_a_long_status_never_pushes_the_clock_or_the_waiting_count_off_the_end(queued):
    now = [1000.0]
    long_status = ("Checking the starting value before any change, which can take a while on a large set of "
                   "files, so please wait for the first reading to finish")

    async def build(app, pilot):
        app._activity.clock = lambda: now[0]
        app._activity.display = True
        app._activity.set_status(long_status)
        app._activity.start_clock()
        app._activity.set_queued(queued)
        now[0] = 1212.0
        await pilot.pause()
        return app._activity.render().plain, app._activity.size.width

    plain, width = run_app(build)
    assert width == 76 and len(plain) <= width, (len(plain), width)
    assert "3m32s" in plain and plain.index("3m32s") < 14, "the clock is read first"
    assert plain.count("…") == 1, "the cut is marked, once"
    if queued:
        assert plain.endswith("· 2 messages waiting")
    else:
        assert plain.rstrip().endswith("…")


def test_a_status_that_fits_is_drawn_whole():
    bar = ActivityBar()
    bar.set_status("Starting the work on sonnet.")
    assert bar.render().plain == "● ● ●  Starting the work on sonnet."


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
        app._handle_event({"type": EVENT_STATUS, "text": "Gathering what I need to start"})
        now[0] = 1046.0
        shown["status"] = app._activity.render().plain
        app._handle_event({"type": EVENT_EXEC, "text": "Reading files",
                           "data": {"run_id": "r1", "goal": "do the work"}})
        now[0] = 1100.0
        shown["exec"] = app._activity.render().plain
        return shown

    shown = run_app(build)
    assert shown["preparing"].endswith("●  45s  Getting ready to start the work…")
    assert shown["status"].endswith("●  46s  Gathering what I need to start"), "a real status replaces the text"
    assert shown["exec"].endswith("●  Working…"), shown["exec"]


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


# --- the order, the arrow, the wrapped structure, and a note that does not capitalise after a colon -------

def test_metric_lines_are_ordered_by_metric_not_by_who_vouches_for_them():
    """The payload lists every measured entry before every reported one; a person reads one metric at a time."""
    entries = [metric_entry(i, name=f"Quality check {i}", source="measured" if i % 3 else "recorded")
               for i in (1, 2, 4, 5, 7, 8, 10, 11, 3, 6, 9)]
    names = [ln.plain.split(":")[0].strip() for ln in metric_lines(entries, limit=None)]
    assert names == [f"Quality check {i}" for i in range(1, 12)], "numbers inside a name compare as numbers"


def test_one_metrics_measured_and_reported_readings_stay_together_in_the_sorted_order():
    entries = [metric_entry(1, name="B metric"), metric_entry(2, name="A metric"),
               metric_entry(1, name="B metric", source="recorded"), metric_entry(2, name="A metric", source="recorded")]
    lines = [ln.plain for ln in metric_lines(entries, limit=None)]
    assert [ln.split(":")[0].strip() for ln in lines] == ["A metric", "B metric"]


def test_the_terminal_writes_the_arrow_the_rest_of_the_app_uses():
    (line,) = metric_lines([metric_entry(0, name="Pass rate")])
    assert "→" in line.plain and "->" not in line.plain


def test_a_not_applicable_reason_opens_its_own_sentence_instead_of_a_capital_after_a_colon():
    (line,) = metric_lines([{"source": "not_applicable", "name": "", "metric_id": "",
                             "reason": "This change only edits a draft note, which no recorded metric covers."}])
    assert line.plain == "  Nothing to measure. This change only edits a draft note, which no recorded metric covers."
    (lower,) = metric_lines([{"source": "not_applicable", "name": "", "metric_id": "",
                              "reason": "no recorded metric covers a draft note"}])
    assert lower.plain == "  Nothing to measure. No recorded metric covers a draft note."
    (acronym,) = metric_lines([{"source": "not_applicable", "name": "", "metric_id": "", "reason": "iOS builds have no meter."}])
    assert acronym.plain == "  Nothing to measure. iOS builds have no meter."
