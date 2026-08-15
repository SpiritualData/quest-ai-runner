"""Per-chat session state + ``ChannelSink`` for the live-channel lane (``channel_runner.py``).

Two things live here:

  * ``ChannelSessionStore`` -- a registry of per-``chat_ref`` conversation history, so the
    orchestrator's User Input Understanding step (anaphora resolution: "ok do it", "the first
    one") has something to resolve against on a channel turn, exactly like the interactive
    terminal session gets. It REUSES ``interactive_session.ChatSessionStore`` (a plain,
    console-independent ``ConversationStore`` over an in-memory turn list) rather than
    reimplementing anaphora resolution -- one ``ChatSessionStore`` per chat, and this registry is
    itself a ``ConversationStore`` that dispatches ``current_slice(conv_id, ...)`` to whichever
    chat's store matches ``conv_id`` (``conv_id`` == ``chat_ref`` by convention in this lane). Wire
    the registry itself as ``RunnerConfig.conversation_store``.

  * ``ChannelSink`` -- messaging POLICY for one turn's stream of ``run_stream`` events, applied by
    ``ChannelRunner``. This is NOT a ``ProgressSink`` (``Orchestrator.run_stream`` builds its own
    internal ``StreamSink`` and does not accept an external sink -- see ``run_stream``'s source);
    instead ``ChannelRunner`` iterates the dict events ``run_stream`` yields and feeds each one to
    ``ChannelSink.on_event`` directly. Policy:

      * a "result" event's text is REMEMBERED (not sent) -- it is the ONLY reliable place a "deep"
        kind's answer text lives; the terminal ``OrchestratorResult`` object's own ``.text`` is
        None for a deep turn (only the "answer" kind populates it -- see
        ``core.orchestrator.Orchestrator._run_deep``'s own message composition, mirrored here).
      * a "decision" event is RELAYED as a message immediately, and IS this turn's terminal reply
        (the spec's "decision-relay" outcome) -- no auto-resolution of Quest decision-requests from
        a channel reply happens here or anywhere in this lane; that is a separate trust decision,
        out of scope.
      * a "milestone" event is throttled to at most one send per ``progress_min_seconds``.
      * everything else (status/plan/read/replan/partial/exec/understanding/context/intent/
        tokens/overseer/mode_signal/card_thread/explanation/done) is DROPPED -- chat-appropriate
        chatter suppression, not a silent-failure risk (the terminal guarantee lives in ``finish``/
        ``error``, which fire independent of what happened during the turn).
      * an optional "still working on it" ACK fires once, ``ack_after_seconds`` after the turn
        started, if nothing terminal has been sent yet.

    ``finish()``/``error()`` are the ONLY two ways a turn ends, and both are idempotent + mutually
    exclusive with each other and with a decision-relay: EXACTLY ONE terminal ``OutboundReply`` is
    ever sent per turn (the terminal guarantee ``channel_runner.py`` depends on). Never raises.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.adapters import (
    EVENT_DECISION,
    EVENT_MILESTONE,
    EVENT_RESULT,
    ChannelTransport,
    ConversationContext,
    OutboundReply,
)
from ..interactive_session import ChatSessionStore

log = logging.getLogger("quest-ai-runner.channel_session")

# How many (user, answer) turns each per-chat ``ChatSessionStore`` keeps in memory. This lane is a
# long-lived process, so unbounded growth per chat would leak; the store's own TF-DF-IDF selection
# already works over a bounded recent window, so trimming here loses nothing that selection would
# have reached anyway.
MAX_TURNS_KEPT = 200

DEFAULT_ACK_AFTER_SECONDS = 15.0
DEFAULT_PROGRESS_MIN_SECONDS = 20.0
DEFAULT_ACK_TEXT = "Still working on it, one moment."
DEFAULT_EMPTY_ANSWER_TEXT = "Done, but there was nothing to report back."
DEFAULT_CANCELLED_TEXT = "That got cancelled before it produced an answer."


class ChannelSessionState:
    """One chat's conversation history + its ``ChatSessionStore`` (anaphora resolution)."""

    def __init__(self, chat_ref: str):
        self.chat_ref = chat_ref
        # ChatSessionStore holds this list BY REFERENCE (see interactive_session.py), so appending
        # here is immediately visible to the store's own current_slice() calls.
        self.history: List[Tuple[str, str]] = []
        self.store = ChatSessionStore(self.history)

    def record_turn(self, user_text: str, answer_text: str) -> None:
        try:
            self.history.append((user_text or "", answer_text or ""))
            if len(self.history) > MAX_TURNS_KEPT:
                del self.history[: len(self.history) - MAX_TURNS_KEPT]
        except Exception:  # noqa: BLE001 — recording history must never break the turn
            log.debug("channel session record_turn failed", exc_info=True)


class ChannelSessionStore:
    """Registry of per-``chat_ref`` ``ChannelSessionState``. ALSO a ``ConversationStore`` (wire the
    instance itself as ``RunnerConfig.conversation_store``): ``current_slice(conv_id, ...)``
    dispatches to the chat whose ``chat_ref == conv_id``. Never raises.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, ChannelSessionState] = {}

    def get_or_create(self, chat_ref: str) -> ChannelSessionState:
        with self._lock:
            state = self._sessions.get(chat_ref)
            if state is None:
                state = ChannelSessionState(chat_ref)
                self._sessions[chat_ref] = state
            return state

    # --- ConversationStore protocol -------------------------------------------------------

    def current_slice(self, conv_id: str, query: str, **kwargs: Any) -> ConversationContext:
        state = self._sessions.get(conv_id)
        if state is None:
            return ConversationContext(scanned=0)
        try:
            return state.store.current_slice(conv_id, query, **kwargs)
        except Exception:  # noqa: BLE001
            log.debug("channel session current_slice failed", exc_info=True)
            return ConversationContext(scanned=0)

    def related_slices(self, query: str, scope: Any, **kwargs: Any) -> ConversationContext:
        # No cross-chat history sharing by design: each chat is its own conversation, and this
        # registry has no notion of "other conversations in scope". A deployment that wants
        # cross-chat recall can wire its own ConversationStore instead of this one.
        return ConversationContext(scanned=0)


class ChannelSink:
    """Messaging policy for ONE turn, driven by ``ChannelRunner`` feeding it ``run_stream`` events.

    See the module docstring for the full policy. Construct one per turn; call ``start()`` before
    the first event, feed events via ``on_event()``, and end with EXACTLY ONE of ``finish()`` /
    ``error()`` (both are safe to call even after a decision already closed the turn -- they just
    no-op).
    """

    def __init__(
        self,
        transport: ChannelTransport,
        chat_ref: str,
        *,
        reply_to_message_id: Optional[str] = None,
        ack_after_seconds: float = DEFAULT_ACK_AFTER_SECONDS,
        progress_min_seconds: float = DEFAULT_PROGRESS_MIN_SECONDS,
        on_send: Optional[Callable[[OutboundReply], None]] = None,
    ):
        self._transport = transport
        self._chat_ref = chat_ref
        self._reply_to = reply_to_message_id
        self._ack_after_seconds = max(0.0, float(ack_after_seconds or 0.0))
        self._progress_min_seconds = max(0.0, float(progress_min_seconds or 0.0))
        # Best-effort observer, purely for tests -- called with every OutboundReply this sink
        # actually sends, right after the send attempt. Never affects control flow.
        self._on_send = on_send

        self._lock = threading.Lock()
        self._terminal_sent = False
        self._closed = False
        self._last_milestone_sent = 0.0
        self._last_result_text: Optional[str] = None
        self._ack_timer: Optional[threading.Timer] = None

    # --- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Arm the "still working" ack timer. Call once, before feeding any events."""
        if self._ack_after_seconds <= 0:
            return
        timer = threading.Timer(self._ack_after_seconds, self._send_ack)
        timer.daemon = True
        self._ack_timer = timer
        timer.start()

    def _cancel_ack(self) -> None:
        timer = self._ack_timer
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _send_ack(self) -> None:
        with self._lock:
            if self._terminal_sent or self._closed:
                return
        self._safe_send(OutboundReply(chat_ref=self._chat_ref, text=DEFAULT_ACK_TEXT,
                                      reply_to_message_id=self._reply_to, kind="ack"))

    # --- event stream --------------------------------------------------------------------

    def on_event(self, event: Dict[str, Any]) -> None:
        """Feed ONE ``run_stream`` event dict. Never raises."""
        try:
            etype = event.get("type")
            if etype == EVENT_RESULT:
                text = event.get("text")
                if text is not None:
                    self._last_result_text = text
            elif etype == EVENT_DECISION:
                self._on_decision(event)
            elif etype == EVENT_MILESTONE:
                self._on_milestone(event)
            # else: chatter for this lane -- dropped by design (see module docstring).
        except Exception:  # noqa: BLE001 — a sink must never break the run it observes
            log.debug("channel sink on_event failed", exc_info=True)

    def _on_decision(self, event: Dict[str, Any]) -> None:
        with self._lock:
            if self._terminal_sent or self._closed:
                return
            self._terminal_sent = True
            self._closed = True
            self._cancel_ack()
        text = (event.get("text") or "").strip() or "I need a decision from you before continuing."
        decision_id = event.get("decision_id")
        if decision_id:
            text = f"{text}\n\n(Decision {decision_id} — resolve it in Quest.)"
        self._safe_send(OutboundReply(chat_ref=self._chat_ref, text=text,
                                      reply_to_message_id=self._reply_to, kind="decision"))

    def _on_milestone(self, event: Dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            if self._terminal_sent or self._closed:
                return
            if now - self._last_milestone_sent < self._progress_min_seconds:
                return
            self._last_milestone_sent = now
        text = (event.get("text") or "").strip()
        if not text:
            return
        self._safe_send(OutboundReply(chat_ref=self._chat_ref, text=text,
                                      reply_to_message_id=self._reply_to, kind="progress"))

    # --- terminal (exactly one of these ever sends) ---------------------------------------

    def finish(self, result: Any) -> Optional[str]:
        """The turn ended with a terminal ``OrchestratorResult``. Sends the final answer UNLESS a
        decision already closed the turn. Returns the text actually sent (or that WOULD have been
        the answer, for ``record_turn``), or None if this call was a no-op. Never raises."""
        with self._lock:
            self._cancel_ack()
            if self._terminal_sent or self._closed:
                self._closed = True
                return None
            self._terminal_sent = True
            self._closed = True
        kind = getattr(result, "kind", None)
        text = (self._last_result_text or getattr(result, "text", None) or "").strip()
        reply_kind = "answer"
        if kind == "cancelled":
            reply_kind = "error"
            text = text or DEFAULT_CANCELLED_TEXT
        elif not text:
            text = DEFAULT_EMPTY_ANSWER_TEXT
        self._safe_send(OutboundReply(chat_ref=self._chat_ref, text=text,
                                      reply_to_message_id=self._reply_to, kind=reply_kind))
        return text

    def error(self, message: str) -> Optional[str]:
        """The turn failed outright (an exception, no result at all). Never raises."""
        with self._lock:
            self._cancel_ack()
            if self._terminal_sent or self._closed:
                self._closed = True
                return None
            self._terminal_sent = True
            self._closed = True
        text = (message or "").strip() or "Something went wrong and I couldn't finish that."
        self._safe_send(OutboundReply(chat_ref=self._chat_ref, text=text,
                                      reply_to_message_id=self._reply_to, kind="error"))
        return text

    @property
    def terminal_sent(self) -> bool:
        with self._lock:
            return self._terminal_sent

    # --- send ------------------------------------------------------------------------------

    def _safe_send(self, reply: OutboundReply) -> None:
        try:
            result = self._transport.send(reply)
            if not result.ok:
                log.warning("channel send failed for chat %s (%s): %s",
                           self._chat_ref, reply.kind, result.error)
        except Exception:  # noqa: BLE001 — transport.send() must never raise, but this is the
                            # backstop: a sink must never propagate a send failure into the run.
            log.error("channel transport.send() raised (should never happen)", exc_info=True)
        if self._on_send is not None:
            try:
                self._on_send(reply)
            except Exception:  # noqa: BLE001 — an observer must never affect control flow
                log.debug("channel sink on_send observer failed", exc_info=True)
