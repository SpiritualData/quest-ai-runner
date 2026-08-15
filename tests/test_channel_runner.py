"""Offline tests for ``runner/channel_runner.py`` and ``runner/channel_session.py``.

No real transport, no real orchestrator, no network. ``ChannelRunner`` accepts both dependencies
injected (``transport=``, ``orchestrator=``), so these tests drive it against small scripted fakes
-- a ``FakeTransport`` implementing ``ChannelTransport`` in memory, and a fake orchestrator whose
``run_stream`` is a plain generator yielding scripted event dicts then a terminal result object
(mirroring ``Orchestrator.run_stream``'s own generator contract: dict events, then one non-dict
terminal item).

Pinned here:
  * an unauthorized sender (not in ``channel_allowed_senders``) gets ZERO orchestrator calls and
    ZERO sends -- fail-closed, spy-verified;
  * an empty allowlist (the default) denies EVERY sender;
  * the same ``(channel, message_id)`` dispatched twice runs the orchestrator exactly once;
  * a second message for a chat ALREADY in flight is folded into ``InputInbox`` instead of
    starting a second concurrent turn, and the running turn actually sees it via its
    ``pending_inputs`` callable;
  * an orchestrator that raises still produces exactly one outbound ERROR reply, and the runner
    keeps working for the next message (the loop survives);
  * ``ChannelSink`` policy: chatter dropped, milestones throttled, a decision is a terminal relay
    (finish() is then a no-op), an ack fires once if the turn runs long, an empty answer degrades
    to a fallback line -- never silence.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.adapters import InboundBatch, InboundMessage, OutboundReply, SendResult
from quest_ai_runner.runner.channel_runner import ChannelRunner
from quest_ai_runner.runner.channel_session import ChannelSink


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeTransport:
    channel: str = "fake"
    sent: List[OutboundReply] = field(default_factory=list)
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def receive(self, *, timeout: float = 25.0) -> InboundBatch:
        return InboundBatch()

    def send(self, reply: OutboundReply) -> SendResult:
        with self._lock:
            self.sent.append(reply)
        return SendResult(ok=True, message_id=f"sent-{len(self.sent)}")

    def close(self) -> None:
        self.closed = True


class Result:
    """Stand-in for ``core.orchestrator.OrchestratorResult`` -- ChannelSink/ChannelRunner only
    read ``.kind`` and ``.text`` off it (via getattr), so a tiny duck-typed object is enough."""

    def __init__(self, kind: str = "answer", text: Optional[str] = None):
        self.kind = kind
        self.text = text


def make_msg(**overrides: Any) -> InboundMessage:
    base: Dict[str, Any] = dict(
        channel="fake", message_id="m1", chat_ref="chat-1", sender_id="alice", text="hi",
    )
    base.update(overrides)
    return InboundMessage(**base)


def make_runner(*, allowed=("alice",), orchestrator=None, transport=None,
                max_workers: int = 2, **cfg_overrides: Any):
    cfg = RunnerConfig(channel_allowed_senders=list(allowed), **cfg_overrides)
    transport = transport if transport is not None else FakeTransport()
    runner = ChannelRunner(cfg, transport=transport, orchestrator=orchestrator,
                           max_workers=max_workers)
    return runner, transport


def wait_until(cond: Callable[[], bool], timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


# ---------------------------------------------------------------------------
# Authorization (fail-closed)
# ---------------------------------------------------------------------------

def test_unauthorized_sender_zero_calls_zero_sends():
    calls: List[Any] = []

    class Orch:
        def run_stream(self, *a: Any, **k: Any):
            calls.append((a, k))
            yield Result(text="should never run")

    runner, transport = make_runner(allowed=("alice",), orchestrator=Orch())
    msg = make_msg(sender_id="mallory")
    accepted = runner._dispatch(msg)
    assert accepted is False
    time.sleep(0.05)  # give any (incorrect) async dispatch a chance to prove itself wrong
    assert calls == []
    assert transport.sent == []
    runner.close()


def test_empty_allowlist_denies_everyone():
    calls: List[Any] = []

    class Orch:
        def run_stream(self, *a: Any, **k: Any):
            calls.append(1)
            yield Result(text="should never run")

    runner, transport = make_runner(allowed=(), orchestrator=Orch())
    accepted = runner._dispatch(make_msg(sender_id="alice"))  # even a "normal" sender is denied
    assert accepted is False
    time.sleep(0.05)
    assert calls == []
    assert transport.sent == []
    runner.close()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def test_duplicate_message_id_exactly_one_turn():
    calls: List[Any] = []

    class Orch:
        def run_stream(self, *a: Any, **k: Any):
            calls.append(1)
            yield Result(text="ok")

    runner, transport = make_runner(orchestrator=Orch())
    msg = make_msg()
    runner._dispatch(msg)
    runner._dispatch(msg)  # exact duplicate: same channel + message_id
    assert wait_until(lambda: len(transport.sent) >= 1)
    time.sleep(0.05)
    assert len(calls) == 1
    assert len(transport.sent) == 1
    runner.close()


# ---------------------------------------------------------------------------
# Mid-turn folding
# ---------------------------------------------------------------------------

def test_mid_turn_message_folds_into_inbox_not_second_turn():
    started = threading.Event()
    release = threading.Event()
    calls: List[str] = []

    class Orch:
        def run_stream(self, user_message: str, *, conv_id=None, attachments=None,
                       pending_inputs=None, **kw: Any):
            calls.append(user_message)
            started.set()
            release.wait(5)
            pending = pending_inputs() if pending_inputs else []
            yield Result(text=f"got pending={pending}")

    runner, transport = make_runner(orchestrator=Orch())
    msg1 = make_msg(message_id="m1", text="first")
    accepted1 = runner._dispatch(msg1)
    assert accepted1 is True
    assert started.wait(2), "the first turn never started"

    msg2 = make_msg(message_id="m2", text="second")  # same chat_ref, still in flight
    accepted2 = runner._dispatch(msg2)
    assert accepted2 is True

    release.set()
    assert wait_until(lambda: len(transport.sent) >= 1)

    # Exactly ONE orchestrator call -- the second message never started its own turn.
    assert calls == ["first"]
    assert len(transport.sent) == 1
    # And it was actually folded in: the running turn's pending_inputs() saw it.
    assert "second" in transport.sent[0].text
    runner.close()


# ---------------------------------------------------------------------------
# Orchestrator failure -> exactly one error reply, loop survives
# ---------------------------------------------------------------------------

def test_orchestrator_raises_one_error_reply_and_loop_survives():
    call_count = {"n": 0}

    class Orch:
        def run_stream(self, *a: Any, **k: Any):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            yield Result(text="second turn ok")  # pragma: no branch

    runner, transport = make_runner(orchestrator=Orch())
    runner._dispatch(make_msg(message_id="m1", chat_ref="chat-1"))
    assert wait_until(lambda: len(transport.sent) == 1)
    assert transport.sent[0].kind == "error"
    assert "boom" in transport.sent[0].text

    # The runner keeps working for a later, unrelated message.
    runner._dispatch(make_msg(message_id="m2", chat_ref="chat-2"))
    assert wait_until(lambda: len(transport.sent) == 2)
    assert transport.sent[1].kind == "answer"
    assert transport.sent[1].text == "second turn ok"
    runner.close()


def test_orchestrator_with_no_terminal_result_still_replies():
    """A run_stream that yields events but never yields a terminal result object (a bug in a
    hypothetical orchestrator) must still produce a terminal reply, never silence."""

    class Orch:
        def run_stream(self, *a: Any, **k: Any):
            yield {"type": "status", "text": "thinking"}
            return

    runner, transport = make_runner(orchestrator=Orch())
    runner._dispatch(make_msg())
    assert wait_until(lambda: len(transport.sent) == 1)
    assert transport.sent[0].kind == "error"
    runner.close()


# ---------------------------------------------------------------------------
# ChannelSink policy
# ---------------------------------------------------------------------------

def test_channel_sink_drops_chatter_throttles_milestones_remembers_result():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", reply_to_message_id="m1",
                       ack_after_seconds=0, progress_min_seconds=1000)
    sink.start()
    events = [
        {"type": "status", "text": "thinking"},             # dropped
        {"type": "plan", "text": "planning"},                # dropped
        {"type": "read", "text": "reading a file"},          # dropped
        {"type": "milestone", "text": "found something"},    # sent (first milestone)
        {"type": "milestone", "text": "found more"},         # throttled
        {"type": "tokens", "data": {}},                      # dropped
        {"type": "result", "text": "final answer text"},     # remembered, not sent directly
    ]
    for ev in events:
        sink.on_event(ev)
    text = sink.finish(Result(kind="answer", text=None))
    assert text == "final answer text"
    kinds = [r.kind for r in transport.sent]
    assert kinds == ["progress", "answer"]
    assert transport.sent[0].text == "found something"
    assert transport.sent[1].text == "final answer text"


def test_channel_sink_decision_is_terminal_and_closes_the_turn():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=0, progress_min_seconds=0)
    sink.start()
    sink.on_event({"type": "decision", "text": "need your ok", "decision_id": "dec-1"})
    sink.on_event({"type": "milestone", "text": "ignored, turn already closed"})
    text = sink.finish(Result(kind="confirm", text=None))
    assert text is None  # finish() is a no-op: the decision already closed the turn
    assert len(transport.sent) == 1
    assert transport.sent[0].kind == "decision"
    assert "dec-1" in transport.sent[0].text


def test_channel_sink_ack_fires_once_then_terminal_still_sends():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=0.05, progress_min_seconds=1000)
    sink.start()
    assert wait_until(lambda: len(transport.sent) == 1, timeout=1.0)
    assert transport.sent[0].kind == "ack"
    sink.finish(Result(kind="answer", text="done"))
    assert len(transport.sent) == 2
    assert transport.sent[1].kind == "answer"
    assert transport.sent[1].text == "done"


def test_channel_sink_ack_does_not_fire_after_early_finish():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=10.0, progress_min_seconds=0)
    sink.start()
    sink.finish(Result(kind="answer", text="fast answer"))
    time.sleep(0.05)
    assert len(transport.sent) == 1
    assert transport.sent[0].kind == "answer"


def test_channel_sink_empty_answer_sends_fallback_never_silence():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=0)
    sink.start()
    sink.finish(Result(kind="answer", text=None))
    assert len(transport.sent) == 1
    assert transport.sent[0].text.strip()


def test_channel_sink_cancelled_result_sends_error_kind():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=0)
    sink.start()
    sink.finish(Result(kind="cancelled", text=None))
    assert transport.sent[0].kind == "error"


def test_channel_sink_error_is_idempotent():
    transport = FakeTransport()
    sink = ChannelSink(transport, "chat-1", ack_after_seconds=0)
    sink.start()
    first = sink.error("boom")
    second = sink.error("boom again")  # must be a no-op: only one terminal reply ever
    assert first == "boom"
    assert second is None
    assert len(transport.sent) == 1
