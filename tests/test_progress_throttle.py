"""Live progress posts are coalesced: a run emitted ~1 event a second, 248 POSTs per 5 minutes per task."""
from hubofhubs.api import ProgressEvent

from quest_ai_runner.consciousness.request_intake.executor_support import _TaskProgressSink


def make(now):
    posts = []
    sink = _TaskProgressSink("t1", lambda tid, kind, text=None, data=None: posts.append((kind, text)),
                             on_milestone=None, clock=lambda: now[0])
    return sink, posts


def ev(text, kind="exec"):
    return ProgressEvent(type=kind, text=text, data=None)


def test_a_burst_posts_once_per_interval_and_keeps_the_latest():
    now = [100.0]
    sink, posts = make(now)
    for i in range(10):
        sink.update(ev(f"step {i}"), None)
        now[0] += 1
    assert posts == [("exec", "step 0")]
    now[0] += 10                          # 20 s since the first post
    sink.update(ev("step 10"), None)
    assert posts[-1] == ("exec", "step 10") and len(posts) == 2


def test_flush_posts_the_pending_latest_and_nothing_when_nothing_is_pending():
    now = [100.0]
    sink, posts = make(now)
    sink.update(ev("a"), None)
    sink.update(ev("b"), None)
    sink.update(ev("c"), None)
    sink.flush()
    assert posts == [("exec", "a"), ("exec", "c")]
    sink.flush()
    assert len(posts) == 2


def test_partials_are_still_skipped():
    now = [100.0]
    sink, posts = make(now)
    sink.update(ev("tok", "partial"), None)
    sink.flush()
    assert posts == []


def test_structured_ticks_post_at_once_and_none_are_merged_away():
    now = [100.0]
    sink, posts = make(now)
    sink.update(ev("step 1"), None)                       # posted, opens the window
    for kind in ("agent", "agent", "spend", "structure", "stop_decision", "milestone", "decision", "tokens"):
        sink.update(ev(kind, kind), None)
        now[0] += 0.5
    assert [k for k, _ in posts] == ["exec", "agent", "agent", "spend", "structure", "stop_decision", "milestone", "decision", "tokens"]


def test_a_held_back_step_goes_out_before_the_structured_tick_that_follows_it():
    now = [100.0]
    sink, posts = make(now)
    sink.update(ev("step 1"), None)
    now[0] += 1
    sink.update(ev("step 2", "read"), None)               # held back
    now[0] += 1
    sink.update(ev("helper started", "agent"), None)
    assert posts == [("exec", "step 1"), ("read", "step 2"), ("agent", "helper started")]
    sink.flush()
    assert len(posts) == 3                                # nothing left pending


def test_flush_due_posts_a_pending_step_only_once_its_window_has_passed():
    now = [100.0]
    sink, posts = make(now)
    sink.update(ev("step 1"), None)
    now[0] += 2
    sink.update(ev("step 2"), None)                       # held back during a long tool call
    sink.flush_due()
    assert posts == [("exec", "step 1")]                  # window not over yet
    now[0] += 14
    sink.flush_due()                                      # 16 s since the last post: the poll posts it
    assert posts == [("exec", "step 1"), ("exec", "step 2")]
    now[0] += 30
    sink.flush_due()                                      # nothing pending: no post
    assert len(posts) == 2


def test_the_executor_polls_flush_due_from_its_stop_checks():
    from quest_ai_runner.consciousness.request_intake.executor import polling_check
    calls = []
    check = polling_check(lambda: False, lambda: calls.append(1))
    assert check() is False and calls == [1]

    def broken():
        raise RuntimeError("post failed")
    assert polling_check(lambda: True, broken)() is True       # a failing post never affects the stop check
