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
