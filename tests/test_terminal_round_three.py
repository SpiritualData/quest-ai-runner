"""The third round of fixes on the terminal chat, from an independent second review of its captured screens.

What a person sees, pinned: the transcript says when there is output below and goes back to it when they send
or the turn ends; wrapped lines keep their structure; the sources are one word with one meaning; every turn ends
on one shape of closing line; statuses end the same way; a cut title is cut at a word.

Ctrl+C, the warnings, the notes typed while the AI works, the usage-limit stop and the metric lines have their
tests in ``test_copy_or_quit``, ``test_terminal_calm_output``, ``test_input_queue_ui``,
``test_terminal_run_outcome`` and ``test_metric_results``.

The real app runs headless under Textual's ``run_test``; no model, no network.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, List

import pytest
from rich.console import Console
from rich.text import Text

from quest_ai_runner.core.adapters_values import DeepResult
from quest_ai_runner.core.adapters_events import EVENT_CONTEXT
from quest_ai_runner.core.inbox import InMemoryInbox
from quest_ai_runner.core.orchestrator import OrchestratorResult
from quest_ai_runner.core.orchestrator_text import ellipsize, sentence
from quest_ai_runner.textual_ui import ActivityBar, PromptTextArea, QuestAITerminal
from quest_ai_runner.textual_ui_helpers import (
    HangingText, clean_title, context_block_lines, indented_line, person_line,
)
from quest_ai_runner.textual_ui_widgets import NEW_OUTPUT_BELOW


class Session:
    """The part of a session the app touches after startup."""
    _rep_name = "Quest AI"
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self.inbox = InMemoryInbox()
        self._cfg = SimpleNamespace(corpus_root="/srv/files/workspace", deep_runner=object())
        self._orch = SimpleNamespace(context_assembler=None, input_inbox=self.inbox)
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self.resumed = None

    def _write_session_file(self) -> None:
        pass

    def _maybe_refresh_next_steps(self, final) -> None:
        pass


def run_app(build, size=(80, 24), session: Any = None):
    async def go():
        app = QuestAITerminal(session if session is not None else Session())
        app._build_session_worker = lambda: None
        app._run_stream = lambda user_text: None
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


def fill(app, n: int) -> None:
    for i in range(n):
        app._tlog.write(Text(f"line {i}"))


def transcript_text(app) -> List[str]:
    return [strip.text.rstrip() for strip in app._tlog.lines]


# --- N10: the transcript says when there is output below, and goes back to it -----------------------

def test_new_output_while_scrolled_up_is_said_in_the_status_strip_and_not_followed():
    async def build(app, pilot):
        fill(app, 80)
        await pilot.pause()
        app._activity.set_status("Working…")
        app._activity.display = True
        before = app._activity.render().plain
        app._tlog.scroll_to(y=0, animate=False)
        await pilot.pause()
        scrolled_to = app._tlog.scroll_y
        app._tlog.write(Text("new line while reading"))
        await pilot.pause()
        return before, app._tlog.following, app._tlog.unseen_below, app._activity.render().plain, \
            app._tlog.scroll_y == scrolled_to

    before, following, unseen, strip, stayed = run_app(build)
    assert NEW_OUTPUT_BELOW == "↓ new output below" and NEW_OUTPUT_BELOW not in before
    assert following is False and unseen is True and stayed, "the view stays where the person put it"
    assert strip.endswith(NEW_OUTPUT_BELOW)


def test_a_blank_line_is_not_new_output():
    async def build(app, pilot):
        fill(app, 80)
        await pilot.pause()
        app._tlog.scroll_to(y=0, animate=False)
        await pilot.pause()
        app._tlog.write(Text(""))
        return app._tlog.unseen_below

    assert run_app(build) is False


def test_sending_a_message_goes_back_to_the_newest_output_and_follows_it_again():
    async def build(app, pilot):
        fill(app, 80)
        await pilot.pause()
        app._tlog.scroll_to(y=0, animate=False)
        await pilot.pause()
        app._tlog.write(Text("unseen"))
        app._begin_turn = lambda text, **kw: None
        prompt = app.query_one("#prompt")
        prompt.post_message(PromptTextArea.Submitted(prompt, "hello"))
        await pilot.pause()
        await pilot.pause()
        log = app._tlog
        return log.following, log.unseen_below, log.scroll_y >= log.max_scroll_y - 1

    following, unseen, at_bottom = run_app(build)
    assert following and not unseen and at_bottom


def test_the_end_of_a_turn_shows_its_end_even_to_someone_who_scrolled_up():
    async def build(app, pilot):
        fill(app, 80)
        await pilot.pause()
        app._tlog.scroll_to(y=0, animate=False)
        await pilot.pause()
        app._begin_turn = lambda text, **kw: None
        app._turn_active = True
        app._finish_turn("hi", OrchestratorResult(kind="answer", text="Here you go.", steps=1, model="sonnet"),
                         3.0, False, None)
        await pilot.pause()
        await pilot.pause()
        log = app._tlog
        return log.following, log.scroll_y >= log.max_scroll_y - 1

    assert run_app(build) == (True, True)


# --- N15: a wrapped line keeps its structure --------------------------------------------------------

def test_a_wrapped_line_starts_its_next_rows_under_its_words():
    rows = person_line("alpha beta gamma delta " * 6).rows(40, Console(width=40))
    assert len(rows) > 2
    assert rows[0].plain.startswith("❯ ") and all(r.plain.startswith("  ") for r in rows[1:])
    assert all(len(r.plain.rstrip()) <= 40 for r in rows)
    assert all(str(r.style) == "bold cyan" for r in rows), "the continuation keeps the line's colour"


def test_a_wrapped_question_carries_its_bar_to_every_row():
    text = "Should I add them to the spreadsheet anyway, or skip them? " * 2
    rows = indented_line(f"  ┃ {text}", "bold yellow", hang=4, carry=True).rows(50, Console(width=50))
    assert len(rows) > 1 and all(r.plain.startswith("  ┃ ") for r in rows)


def test_a_line_in_a_terminal_too_narrow_to_indent_is_left_for_the_log_to_wrap():
    line = person_line("x " * 40)
    assert line.rows(8, Console(width=8)) == [line]


def test_a_paragraph_break_inside_a_line_keeps_the_indent():
    rows = person_line("first part\nsecond part").rows(40, Console(width=40))
    assert [r.plain.rstrip() for r in rows] == ["❯ first part", "  second part"]


def test_in_the_running_app_a_long_message_wraps_under_its_words_and_keeps_its_style():
    async def build(app, pilot):
        app._tlog.write(person_line("word " * 40))
        await pilot.pause()
        return transcript_text(app)

    rows = [r for r in run_app(build) if r.strip()]
    start = next(i for i, r in enumerate(rows) if r.startswith("❯ "))
    block = rows[start:]
    assert len(block) >= 3 and all(r.startswith("  ") for r in block[1:])


def test_a_hanging_line_survives_the_log_deferring_it_until_it_has_a_size():
    line = indented_line("  ◆ alpha", "italic", hang=4)
    assert isinstance(line.copy(), HangingText) and line.copy().hang == 4


def test_the_question_is_written_with_its_bar_on_every_row_at_80_columns():
    async def build(app, pilot):
        app.sess = Session()
        app.pending_question = "14 of the opportunities I found are on the quest's exclusion list. Should I add them anyway, or skip them?"
        app.write_pending_question(OrchestratorResult(kind="confirm", question="x", decision_id="d"))
        await pilot.pause()
        return transcript_text(app)

    rows = [r for r in run_app(build) if "┃" in r or r.strip().startswith(("or ", "skip", "I add"))]
    assert len(rows) >= 2 and all(r.lstrip().startswith("┃") for r in rows)


# --- N4: one word for sources, and what each count means -----------------------------------------------

def test_the_context_line_says_looked_at_never_found_or_used():
    cards = [{"title": f"Source {i}", "relevance_score": i} for i in range(6)]
    (line,) = context_block_lines(cards, [])
    assert line.plain == "  Looked at 6 sources: Source 5, Source 4 and 4 more."
    assert "used" not in line.plain and "Found" not in line.plain


def test_a_title_loses_the_kind_of_record_it_came_from_and_is_cut_at_a_word_without_stray_punctuation():
    assert clean_title("Quest: The Psychic Journeys Documentary Series") == "The Psychic Journeys Documentary Series"
    assert clean_title("collection:   Pricing tiers") == "Pricing tiers"
    assert clean_title("Goal: Become a Machine Learning Engineer") == "Become a Machine Learning Engineer"
    assert clean_title("Quest:") == "Quest:" or clean_title("Quest: ") == "Untitled source"
    assert clean_title(None) == "Untitled source"
    long = "Quest: The Psychic Journeys Documentary Series, Research Notes, Interview Schedule and Distribution Plan"
    (line,) = context_block_lines([{"title": long, "relevance_score": 1}, {"title": "GCP Service Account", "relevance_score": 0.5}], [])
    assert ",…," not in line.plain and "Quest:" not in line.plain
    assert "Series…" in line.plain or "Series," in line.plain
    shown = line.plain.split(": ", 1)[1]
    assert shown.split(" and ")[0].count("…") <= 1


def test_ellipsize_cuts_at_a_word_and_drops_the_punctuation_it_leaves():
    assert ellipsize("The Psychic Journeys Documentary Series, Research Notes", 40) == "The Psychic Journeys Documentary Series…"
    assert ellipsize("short", 40) == "short"
    cut = ellipsize("Add relevant ML engineer opportunities to the spreadsheet using the service account", 60)
    assert cut.endswith("…") and " " not in cut[-3:] and len(cut) <= 60
    assert not cut[:-1].endswith(("the", "us")) or cut[:-1].split()[-1] in {"the", "using"}, "a whole word"


def test_a_status_sentence_ends_in_a_full_stop_unless_it_already_ends_in_closing_punctuation():
    assert sentence("Gathering what I need to start") == "Gathering what I need to start."
    assert sentence("Working on: Add the rows…") == "Working on: Add the rows…"
    assert sentence("Already done.") == "Already done."
    assert sentence("") == ""


def test_the_panel_for_a_short_answer_says_looked_at_and_a_workers_own_list_says_relied_on():
    async def build(app, pilot):
        app._ctx.set_cards([{"id": "c1", "title": "Quest: Pricing tiers", "adapter": "keyword"}])
        app._future_context = ""
        app._answer_parts = []
        app._auto_pass = 1
        app._finish_turn("q", OrchestratorResult(kind="answer", text="Yes.", steps=1, model="sonnet"), 1.0, False, None)
        app.action_toggle_future_context()
        await pilot.pause()
        short = str(app._future_ctx_panel._body.render())
        app._begin_turn = lambda text, **kw: None
        app._future_ctx_panel.hide()
        app._future_context = "- collection: Pricing tiers\n- Refund policy"
        app._finish_turn("q", OrchestratorResult(kind="answer", text="Yes.", steps=1, model="sonnet"), 1.0, False, None)
        app.action_toggle_future_context()
        await pilot.pause()
        return short, str(app._future_ctx_panel._body.render())

    short, flagged = run_app(build)
    assert short.startswith("Sources looked at (1)") and "Quest:" not in short
    assert flagged.startswith("Sources it relied on (2)")


def test_how_i_got_this_counts_the_jobs_actions_as_the_summary_line_does_and_metric_lines_not_entries():
    async def build(app, pilot):
        app._deep.add_run("r1", "Do the work")
        for line in ["Read: a.md", "Read: b.md", "Edit: c.md", "$ ls", "WebSearch: x", "Using Agent",
                     "[thinking] hmm", "I planned the change."]:
            app._deep.update_run_output("r1", line)
        app._deep.set_final_output("r1", "Changed it.")
        app._flush_deep_run("r1")
        summary = [str(s.text).strip() for s in app._tlog.lines if "files read" in str(s.text)]
        entries = [
            {"solution_id": "s", "metric_id": "m", "name": "Pass rate", "source": "measured", "before": 0.5, "after": 0.75,
             "disagrees": False},
            {"solution_id": "s", "metric_id": "m", "name": "Pass rate", "source": "recorded", "before": 0.5, "after": 0.75,
             "disagrees": False},
        ]
        final = OrchestratorResult(kind="deep", goals=["g"], steps=3, model="sonnet",
                                   deep_results=[DeepResult(met=True, output="Changed it.", model="sonnet")])
        final.explanation = {"version": 3, "used": {"cards": [{"title": "T", "adapter": "files"}] * 6, "sources": [],
                                                     "actions": [{"goal": "g", "state": "succeeded"}] * 3, "web": False},
                             "metrics": entries, "metric_missing": False}
        app._auto_pass = 1
        app._turn_active = True
        app._finish_turn("do it", final, 5.0, False, None)
        await pilot.pause()
        return summary, transcript_text(app)

    summary, rows = run_app(build)
    assert summary == ["2 files read · 1 edit · 1 command · 1 search · 1 tool call"]
    explain = next(r for r in rows if "How I got this" in r)
    assert explain.strip() == "How I got this: 6 sources, 6 actions, 1 metric result", \
        "6 actions are the 2 reads, 1 edit, 1 command, 1 search and 1 tool call the summary line lists"


# --- N9: one shape of closing line -------------------------------------------------------------------

def finish_and_read_closing(final, elapsed, *, verbosity=0, before=None, size=(100, 30)):
    async def build(app, pilot):
        app._auto_pass = 1
        app._turn_active = True
        if before:
            before(app)
        app._finish_turn("hi", final, elapsed, False, None)
        await pilot.pause()
        return [r.strip() for r in transcript_text(app)
                if r.strip().startswith(("Answered in", "✓ Done", "~ ", "✗ ", "Waiting"))]

    async def go():
        app = QuestAITerminal(Session(), verbosity)
        app._build_session_worker = lambda: None
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


def test_a_plain_answer_and_a_finished_job_end_on_the_same_shape_of_line():
    answer = OrchestratorResult(kind="answer", text="Done.", steps=2, model="sonnet", tokens_in=1200, tokens_out=300)
    (quiet,) = finish_and_read_closing(answer, 9.0)
    assert quiet == "Answered in 9s · sonnet", "no step count, source count or tokens by default"
    deep = OrchestratorResult(kind="deep", goals=["g"], steps=3, model="sonnet",
                              deep_results=[DeepResult(met=True, output="x", model="sonnet", tokens=18200)])
    (finished,) = finish_and_read_closing(deep, 212.0)
    assert finished == "✓ Done in 3m32s · sonnet"


def test_steps_replans_tokens_and_the_full_model_id_are_for_verbose():
    answer = OrchestratorResult(kind="answer", text="Done.", steps=2, model="claude-sonnet-5-5", tokens_in=1200,
                                tokens_out=300)

    def replans(app):
        app._ctx.inc_replans()
        app._ctx.add_sources(["a.md", "b.md"])
    (verbose,) = finish_and_read_closing(answer, 9.0, verbosity=1, before=replans, size=(160, 30))
    assert verbose.startswith("Answered in 9s · 2 steps · 2 files read · 1 replan · ↥ 1.2k in · ↦ 300 out")


def test_the_hint_words_name_sources_and_details():
    answer = OrchestratorResult(kind="answer", text="Done.", steps=2, model="sonnet")
    (line,) = finish_and_read_closing(answer, 9.0, before=lambda app: (
        app._ctx.set_cards([{"id": "c", "title": "T", "adapter": "files", "relevance_score": 1}])))
    assert line == "Answered in 9s · sonnet · Alt+C sources"


# --- N16, N17: start-up wording ---------------------------------------------------------------------

def test_the_header_while_the_session_builds_says_starting_up():
    from quest_ai_runner import interactive_session_persona
    import inspect
    assert 'notify("Starting up…")' in inspect.getsource(interactive_session_persona)
    assert "Resolving AI persona" not in inspect.getsource(interactive_session_persona)


def test_picking_up_a_conversation_counts_messages_not_turns(tmp_path):
    from quest_ai_runner import chat_conversations

    history = [(f"question {i}", f"answer {i}") for i in range(12)] + [("last question", "")]
    path = tmp_path / (chat_conversations.FILE_PREFIX + "20261020-1142-ab12cd34.json")
    conv = chat_conversations.SavedConversation(path=path, history=history, meta={"rep_name": "Quest AI"})

    async def go():
        app = QuestAITerminal(None, _config=None, resume=conv)
        app._build_session_worker = lambda: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            return "\n".join(r for r in transcript_text(app))

    text = asyncio.run(go())
    assert "Picking up your last conversation (25 messages)." in text
    assert "6 earlier messages not shown." in text, "the three earliest exchanges, as messages"
    assert "turn" not in text.lower().split("picking up")[1].split("\n")[0]


# --- N2 in the running app: a new turn starts its own count of warnings ---------------------------------

def test_in_the_running_app_each_turn_gets_its_own_warning_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("QAR_CHAT_LOG_FILE", str(tmp_path / "chat.log"))

    async def build(app, pilot):
        app._build_session_worker = lambda: None
        log = logging.getLogger("quest_ai_runner.test_round_three")
        for n in range(5):
            log.warning("First turn warning %d.", n)
        await pilot.pause()
        app.sess = app.sess or Session()
        app.log_handler.write_summary()
        app._begin_turn("second message", echo=True)
        for n in range(5):
            log.warning("Second turn warning %d.", n)
        await pilot.pause()
        app.log_handler.write_summary()
        await pilot.pause()
        return [r.strip() for r in transcript_text(app) if r.strip()]

    async def go():
        app = QuestAITerminal(Session())
        app._build_session_worker = lambda: None
        app._run_stream = lambda user_text: None
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            return await build(app, pilot)

    rows = asyncio.run(go())
    first = [r for r in rows if r.startswith("⚠ First")]
    second = [r for r in rows if r.startswith("⚠ Second")]
    assert len(first) == 3 and len(second) == 3
    assert sum("2 more notices." in r for r in rows) == 2
    at = rows.index("❯ second message")
    assert "chat.log" in " ".join(rows[at - 2:at]), "the first burst is closed (Details: <log path>) before the next message"
