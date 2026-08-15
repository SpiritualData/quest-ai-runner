"""ChannelRunner -- the loop that drives ONE live, two-way ``ChannelTransport``.

Structurally mirrors ``runner.poller.Poller._fast_lane_loop``'s never-die shape (long-poll,
fast-failure backoff, one bad iteration never kills the process) but for a channel instead of the
Quest task queue: receive a batch, AUTHORIZE + DEDUP each message, run at most one orchestrator
turn per ``chat_ref`` at a time (a message that arrives mid-turn folds into the running turn via
``InputInbox`` instead of starting a second one), and guarantee exactly one terminal reply per
turn (see ``channel_session.ChannelSink``).

This is its OWN process/entry point (``cli.py``'s ``channel`` subcommand), separate from
``poller.py``'s background scan and from ``TaskExecutor``: a chat message has no Quest task id to
claim/PATCH, so forcing it through the task executor would invent a task for something that isn't
one. The two lanes share only the dedup mechanism (``runner.state_store.StateStore``) and, when a
consumer wires both, the same Quest connection / model provider via ``RunnerConfig``.

AUTHORIZATION is a plain membership test against ``RunnerConfig.channel_allowed_senders`` --
operator-configured sender ids, checked against ``InboundMessage.sender_id`` (a field the
TRANSPORT reports from the channel's own protocol, not text any model generated). An EMPTY
allowlist means DENY ALL (fail closed): an unconfigured deployment authorizes nobody rather than
everybody. This is a real security boundary, not hygiene -- see ``core.adapters.ChannelTransport``
and ``adapters.openclaw_channel`` for why (OpenClaw's own history of exploitable exposure).
``EVENT_DECISION`` events are relayed to the chat as a message; this lane NEVER auto-resolves a
Quest decision-request from a channel reply -- that is a separate trust decision, out of scope.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..config import RunnerConfig, build_orchestrator
from ..core.adapters import ChannelTransport, InboundBatch, InboundMessage
from ..core.inbox import InMemoryInbox
from .channel_session import ChannelSessionStore, ChannelSink
from .state_store import StateStore

log = logging.getLogger("quest-ai-runner.channel_runner")

# How long one ``transport.receive()`` call is allowed to block, absent an explicit override.
# Matches ``Poller``'s ``wait_timeout_seconds`` default (poller.py) -- the same "hold a long-poll
# open, reconnect immediately after each return" shape, just for a channel instead of the Quest
# task-wait endpoint.
DEFAULT_RECEIVE_TIMEOUT_SECONDS = 25.0

# A receive() call that errors out well under its own timeout looks like a broken transport, not
# an ordinary empty long-poll -- back off briefly so it can never spin in a tight retry loop.
FAST_FAILURE_THRESHOLD_SECONDS = 1.0
FAST_FAILURE_BACKOFF_SECONDS = 5.0


class ChannelRunner:
    """Drives ONE ``ChannelTransport`` to completion for as many turns as arrive.

    ``transport`` defaults to ``config.channel_transport`` (``None`` = the lane has nothing to do
    -- ``run_forever``/``run_once`` degrade to a clean no-op, exactly like the poller's own
    "unconfigured key -> log + exit" discipline). ``orchestrator``/``session_store`` are
    injectable for tests; production callers leave them unset and get the config-built defaults.
    """

    def __init__(
        self,
        config: RunnerConfig,
        *,
        transport: Optional[ChannelTransport] = None,
        orchestrator: Optional[Any] = None,
        session_store: Optional[ChannelSessionStore] = None,
        state_path: Optional[str] = None,
        max_workers: int = 4,
        receive_timeout_seconds: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
    ):
        self.cfg = config
        self.transport = transport if transport is not None else config.channel_transport
        # FAIL CLOSED: an empty/unset allowlist authorizes NOBODY. See the module docstring.
        self.allowed_senders = frozenset(
            str(s) for s in (config.channel_allowed_senders or []) if str(s).strip())
        self.state = StateStore(state_path if state_path is not None else config.channel_state_path)
        self.sessions = session_store or ChannelSessionStore()
        # Give the orchestrator's anaphora resolution (Step 1) something to resolve against, the
        # same way the interactive terminal session does -- but only if the consumer didn't
        # already wire their OWN ConversationStore (an explicit choice always wins).
        if self.cfg.conversation_store is None:
            self.cfg.conversation_store = self.sessions
        self._orchestrator = orchestrator
        self._inbox = InMemoryInbox()
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        self._inflight_lock = threading.Lock()
        self._inflight_chats: set = set()
        self.receive_timeout_seconds = receive_timeout_seconds

    def _orch(self) -> Any:
        if self._orchestrator is None:
            self._orchestrator = build_orchestrator(self.cfg)
        return self._orchestrator

    # --- one receive -> dispatch pass ---------------------------------------------------------

    def run_once(self, *, timeout: Optional[float] = None) -> int:
        """One ``receive()`` call + dispatch of whatever it returned. Returns the number of
        messages dispatched (authorized, deduped, and either started as a new turn or folded into
        one already running). Never raises."""
        if self.transport is None:
            return 0
        try:
            batch = self.transport.receive(timeout=timeout if timeout is not None
                                           else self.receive_timeout_seconds)
        except Exception:  # noqa: BLE001 -- receive() must never raise; this is the backstop
            log.error("channel transport.receive() raised (should never happen)", exc_info=True)
            return 0
        if not isinstance(batch, InboundBatch):
            log.error("channel transport.receive() returned %r, not an InboundBatch", type(batch))
            return 0
        if batch.error:
            (log.warning if not batch.healthy else log.info)(
                "channel receive: %s", batch.error)
            return 0
        dispatched = 0
        for msg in batch.messages:
            try:
                if self._dispatch(msg):
                    dispatched += 1
            except Exception:  # noqa: BLE001 -- one bad message must never sink the batch
                log.error("channel dispatch crashed for a message (should never happen)",
                         exc_info=True)
        return dispatched

    def _dispatch(self, msg: InboundMessage) -> bool:
        """Authorize, dedup, and either start a new turn or fold into one in flight. Returns True
        if the message was accepted (started a turn OR was folded into a running one)."""
        if not msg.chat_ref or not msg.message_id:
            log.warning("channel message missing chat_ref/message_id; dropping")
            return False
        sig = f"{msg.channel}:{msg.message_id}"
        if self.state.seen(sig):
            log.debug("channel: duplicate message %s; skipping", sig)
            return False
        # Plain membership test against OPERATOR config -- never a decision made by scanning any
        # model-generated text (hard rule #3). See the module docstring.
        if not msg.sender_id or msg.sender_id not in self.allowed_senders:
            log.warning(
                "channel: rejecting message %s from unauthorized sender %r on chat %r (%s)",
                msg.message_id, msg.sender_id, msg.chat_ref, msg.channel)
            self.state.mark(sig)  # dedup the rejection too -- a redelivered event must not re-log
            return False
        self.state.mark(sig)

        with self._inflight_lock:
            in_flight = msg.chat_ref in self._inflight_chats
            if not in_flight:
                self._inflight_chats.add(msg.chat_ref)
        if in_flight:
            self._inbox.push(msg.chat_ref, msg.text)
            log.info("channel: folded message %s into the in-flight turn for chat %s",
                     msg.message_id, msg.chat_ref)
            return True

        self._pool.submit(self._run_turn_guarded, msg)
        return True

    # --- one turn --------------------------------------------------------------------------

    def _run_turn_guarded(self, msg: InboundMessage) -> None:
        try:
            self._run_turn(msg)
        except Exception:  # noqa: BLE001 -- _run_turn already guards itself; this is the backstop
            log.error("channel turn crashed outside its own guard (should never happen) for "
                     "chat %s", msg.chat_ref, exc_info=True)
        finally:
            with self._inflight_lock:
                self._inflight_chats.discard(msg.chat_ref)

    def _run_turn(self, msg: InboundMessage) -> None:
        """Run ONE orchestrator turn for ``msg`` and guarantee exactly one terminal reply.

        Whatever happens -- a clean answer, a deep run, a raised decision, an unexpected exception
        from the orchestrator itself, or the configured turn timeout -- something is always sent
        back (``ChannelSink.finish``/``error``), and this method never raises: the caller
        (``_run_turn_guarded``) has a backstop too, but the terminal guarantee is enforced HERE.
        """
        sink = ChannelSink(
            self.transport, msg.chat_ref, reply_to_message_id=msg.message_id,
            ack_after_seconds=self.cfg.channel_ack_after_seconds,
            progress_min_seconds=self.cfg.channel_progress_min_seconds,
        )
        sink.start()
        session = self.sessions.get_or_create(msg.chat_ref)
        answer_text: Optional[str] = None
        try:
            orch = self._orch()
            turn_deadline: Optional[float] = None
            if self.cfg.channel_turn_timeout_seconds and self.cfg.channel_turn_timeout_seconds > 0:
                turn_deadline = time.monotonic() + self.cfg.channel_turn_timeout_seconds

            terminal_result: Any = None
            for item in orch.run_stream(
                msg.text,
                conv_id=msg.chat_ref,
                attachments=(msg.attachments or None),
                pending_inputs=lambda: self._inbox.drain(msg.chat_ref),
            ):
                if isinstance(item, dict):
                    sink.on_event(item)
                else:
                    terminal_result = item
                    break
                if turn_deadline is not None and time.monotonic() > turn_deadline:
                    answer_text = sink.error(
                        "This is taking longer than the configured channel turn timeout. "
                        "Check Quest for progress, or try again.")
                    return

            if terminal_result is not None:
                answer_text = sink.finish(terminal_result)
            else:
                answer_text = sink.error("The assistant ended this turn with no result.")
        except Exception as e:  # noqa: BLE001 — THE terminal guarantee: whatever goes wrong here,
                                  # the human gets something back, and the loop keeps running.
            log.error("channel turn crashed for chat %s: %s", msg.chat_ref, e, exc_info=True)
            answer_text = sink.error(f"Something went wrong on my end: {type(e).__name__}: {e}")
        finally:
            session.record_turn(msg.text, answer_text or "")

    # --- run modes -----------------------------------------------------------------------

    def run_forever(self, *, stop_event: Optional[threading.Event] = None) -> None:
        """Loop ``run_once()`` forever (or until ``stop_event`` is set). Never raises; one bad
        iteration is logged and the loop continues, exactly like ``Poller._fast_lane_loop``."""
        if self.transport is None:
            log.info("no channel transport configured — the channel runner has nothing to do")
            return
        if stop_event is None:
            stop_event = threading.Event()
        try:
            while not stop_event.is_set():
                started = time.monotonic()
                try:
                    dispatched = self.run_once()
                except Exception:  # noqa: BLE001 — run_once() already guards itself; backstop
                    log.error("channel run_once() raised (should never happen)", exc_info=True)
                    dispatched = 0
                elapsed = time.monotonic() - started
                if dispatched == 0 and elapsed < FAST_FAILURE_THRESHOLD_SECONDS:
                    # Looks like a fast failure, not a clean ~timeout-length empty long-poll.
                    if stop_event.wait(FAST_FAILURE_BACKOFF_SECONDS):
                        return
                # else: a normal empty long-poll return, or messages were handled — reconnect
                # immediately, no extra sleep (the long-poll itself provides the pacing).
        finally:
            self.close()

    def close(self) -> None:
        """Best-effort teardown: stop accepting new turns and close the transport. Never raises."""
        try:
            self._pool.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            log.debug("channel runner pool shutdown failed", exc_info=True)
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001 — close() must never raise, but this is the backstop
                log.debug("channel transport close() raised (ignored)", exc_info=True)
