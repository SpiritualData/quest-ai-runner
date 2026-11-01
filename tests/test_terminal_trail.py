"""The terminal shows the AI's work, its earlier updates, its questions and a stopped reply, drawn from the hub's renderers.

Drives the real app headless (``run_test``) with stand-in events: no model, no network. What is shown and why is the hub's
(``hubofhubs/tests/test_terminal_render.py``); these tests pin that this terminal draws it, in place, and binds the keys.
"""
import pytest

from tests.terminal_trail_support import Harness, FakeClient, ask_event, interim, result_event, site, trace

pytestmark = pytest.mark.asyncio


def text_of(h):
    """The transcript as one line of text, so a sentence the log wrapped is still found whole."""
    return " ".join(" ".join(line.split()) for line in h.lines() if line.strip())


async def test_a_transcript_place_is_rewritten_without_moving_the_rest():
    async with Harness() as h:
        log = h.app._tlog
        log.clear()
        from rich.text import Text
        log.put("a", [Text("first")])
        log.put("b", [Text("second"), Text("second too")])
        log.put("c", [Text("third")])
        log.put("a", [Text("first one"), Text("first two")])      # grows: later places shift down
        log.put("b", [])                                          # taken away
        assert [t for t in h.lines() if t] == ["first one", "first two", "third"]
        log.put("c", [Text("third again")])
        assert h.lines()[-1] == "third again"
        log.put("b", [Text("back")])                              # a removed place is written at the end
        assert h.lines()[-1] == "back"


async def test_the_live_row_names_the_newest_page_and_goes_when_the_turn_ends():
    async with Harness() as h:
        h.begin()
        h.feed(trace("w", items=[]))
        assert not h.app._trail_line.display
        h.feed(trace("w", items=[site("a.org"), site("https://www.blog.rust-lang.org/x?q=1")], read=2))
        assert h.app._trail_line.display and "Opened blog.rust-lang.org" in str(h.app._trail_line.render())
        h.feed(trace("w", state="done", items=[site("a.org"), site("blog.rust-lang.org", "Rust blog")], read=2))
        h.finish()
        assert not h.app._trail_line.display
        summary = [l for l in h.lines() if l.strip().startswith("Read a.org")]
        assert summary and "Alt+T" in summary[0]


async def test_alt_t_opens_the_groups_in_place_and_closes_them():
    async with Harness() as h:
        h.begin()
        pages = [site(f"s{i}.org", f"Page {i}") for i in range(7)]
        h.feed(trace("w", state="done", read=7, items=pages))
        h.finish()
        before = len(h.lines())
        await h.pilot.press("alt+t")
        opened = text_of(h)
        assert "s0.org Page 0" in opened and "s4.org" in opened and "s5.org" not in opened and "Show all 7" in opened
        await h.pilot.press("alt+t")
        assert "s6.org Page 6" in text_of(h)
        await h.pilot.press("alt+t")
        assert "s0.org Page 0" not in text_of(h) and len(h.lines()) == before


async def test_interims_fold_into_one_row_and_alt_u_opens_it():
    async with Harness() as h:
        h.begin()
        h.feed(interim("Read 3 of 9 files. More follows.", "six left", "read the rest"))
        assert "Progress so far" in text_of(h) and "Still needed: six left" in text_of(h)
        h.feed(interim("Read 6 of 9 files.", "three left"))
        out = text_of(h)
        assert "Earlier updates (1)" in out and "Read 3 of 9 files." in out       # the preview
        assert "Still needed: six left" not in out and "Still needed: three left" in out
        h.finish("All nine files are read.")
        out = text_of(h)
        assert "Earlier updates (2)" in out and out.index("Earlier updates (2)") < out.index("All nine files are read.")
        assert "Still needed: three left" not in out                              # the final is the open message
        await h.pilot.press("alt+u")
        assert "Still needed: three left" in text_of(h)
        await h.pilot.press("alt+u")
        assert "Still needed: three left" not in text_of(h)


async def test_an_ask_is_the_end_of_the_turn_and_answered_by_number_through_the_client():
    client = FakeClient()
    async with Harness(client) as h:
        h.begin()
        began = []
        h.app._begin_turn = lambda text, **kw: began.append(text)
        h.feed(ask_event())
        assert "Which spreadsheet" not in text_of(h)                       # written once, as the last thing
        h.finish("I read both sheets.\n\nWhich spreadsheet should I use?", kind="answer")
        out = text_of(h)
        assert out.count("Which spreadsheet should I use?") == 1
        assert "Needs you" in out and "1. The 2025 sheet" in out and "/notnow keeps this open" in out
        assert h.app._awaiting_decision
        h.app._turn_active = False
        assert h.app.open_ask_takes("2")
        await h.pilot.pause(0.3)
        assert client.calls == [("dec_1", "answered", "The 2026 sheet")]
        assert "You answered. Answer saved." in text_of(h) and began == ["The 2026 sheet"]
        assert h.app.term.ask is None


async def test_not_now_folds_the_question_and_keeps_it_open():
    client = FakeClient()
    async with Harness(client) as h:
        h.begin()
        h.feed(ask_event())
        h.finish("x")
        h.app._turn_active = False
        h.app._dispatch_command("/notnow")
        out = text_of(h)
        assert "Waiting for your answer" in out and "Needs you" not in out
        assert h.app.term.ask is not None and not h.app._awaiting_decision
        assert not h.app.open_ask_takes("hello")                           # a plain message again
        h.app._dispatch_command("/answer")
        assert "Needs you" in text_of(h) and h.app._awaiting_decision
        assert client.calls == []


async def test_an_answer_that_could_not_be_sent_says_so_and_stays_open():
    async with Harness(FakeClient(fail=True)) as h:
        h.begin()
        h.feed(ask_event())
        h.finish("x")
        h.app._turn_active = False
        h.app.open_ask_takes("1")
        await h.pilot.pause(0.3)
        assert "Could not send that. Try again." in text_of(h) and h.app.term.ask is not None


async def test_an_ask_for_a_teammate_says_who_it_waits_on_and_takes_no_answer():
    async with Harness(FakeClient()) as h:
        h.begin()
        h.feed(ask_event(assigned_to="u9", assigned_to_name="Sam", asker_user_id="u1"))
        h.finish("x")
        h.app._turn_active = False
        out = text_of(h)
        assert "Waiting on Sam" in out and "Needs you" not in out
        assert not h.app.open_ask_takes("1")


async def test_a_stopped_reply_gives_the_plain_reason_and_alt_k_continues():
    async with Harness() as h:
        h.begin()
        h.feed(result_event("stopped", "stalled"))
        h.finish("Here is what I have so far.")
        out = text_of(h)
        assert "I stopped here because my last few tries were not getting anywhere." in out and "Alt+K continue" in out
        began = []
        h.app._begin_turn = lambda text, **kw: began.append((text, kw))
        h.app._turn_active = False
        await h.pilot.press("alt+k")
        assert began and began[0][0] == "Continue" and not h.app.continue_offer


async def test_a_met_reply_shows_no_stopped_line_and_a_plain_chat_shows_no_work_line():
    async with Harness() as h:
        h.begin()
        h.feed(result_event("met"))
        h.finish("Hello.")
        out = text_of(h)
        assert "I stopped here" not in out and "Read " not in out and "Earlier updates" not in out


async def test_the_turn_asks_for_the_trace_and_the_terminal_owns_privacy_as_the_asker(monkeypatch):
    async with Harness() as h:
        h.begin()
        meta = h.app.term_turn_meta({"quest_ids": ["q"]})
        assert meta["work_trace"]["audience"] == "owner" and meta["quest_ids"] == ["q"] and meta["turn_id"] == h.app.term.turn_id
        assert h.app.sess._orch.cfg.interim_replies is False
        monkeypatch.setenv("QAR_INTERIM_REPLIES", "1")
        h.app.term_turn_meta(None)
        assert h.app.sess._orch.cfg.interim_replies is True
