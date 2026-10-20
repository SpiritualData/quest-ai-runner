"""Steer, stop and pause a running deep worker from the terminal.

``TerminalRunControl`` is the terminal's ``core.run_control.RunControl`` for ONE turn. A deep worker
that holds a live session (the stream-json runner) asks it, a few times a second, for new messages
and for a stop or a pause; a worker that does not name a ``control`` keyword (the one-shot
subprocess runner) is never handed it, so for that worker nothing here runs and a queued message
behaves exactly as before: the orchestrator's own drains and the end-of-turn flush pick it up.

Delivery is exactly once because everything draws from the SAME ``InputInbox`` that the typed
"queued" line pushed into, and ``drain`` is atomic: a message is taken by the live worker, or by the
orchestrator's own loop, or by the end-of-turn flush, never by two of them. A message the worker took
but never confirmed (``DeepResult.unseen_inputs``, or a claim with no ``mark_seen``) is put back at
the FRONT of the inbox when the turn ends, so the end-of-turn flush sends it again.

``ControlMixin`` is the ``QuestAITerminal`` side: the ``/pause`` and ``/resume`` commands and the
transcript lines that make delivery visible. Methods only; state lives on the instance.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, Iterable, List, Optional

from rich.text import Text

from .core.run_control import ControlMessage, RunControl
from .textual_ui_prompt import PromptTextArea
from .textual_ui_widgets import _busy_placeholder

# How often a live worker looks for a typed message. Short, because a person is waiting at the
# prompt for their words to reach the model.
TERMINAL_POLL_SECONDS = 1.0


def _noop(*args, **kwargs) -> None:
    return None


class TerminalRunControl:
    """The control channel of one terminal turn, bound to this session's inbox."""

    def __init__(self, inbox, session_key: str, *,
                 cancel_check: Callable[[], bool],
                 on_sent: Callable[[List[str], int], None] = _noop,
                 on_seen: Callable[[List[str]], None] = _noop,
                 on_live: Callable[[], None] = _noop,
                 poll_seconds: float = TERMINAL_POLL_SECONDS) -> None:
        self.inbox = inbox
        self.session_key = session_key
        self.pause_event = threading.Event()
        self.poll_seconds = poll_seconds
        self._cancel_check = cancel_check
        self._on_sent = on_sent
        self._on_seen = on_seen
        self._on_live = on_live
        self._lock = threading.Lock()
        self._counter = 0
        # id -> text of every message a worker took and has not yet confirmed.
        self._unconfirmed: Dict[str, str] = {}
        # Every text taken this turn (confirmed or not), to tell a worker's own report from ours.
        self._taken_texts: List[str] = []
        self._live = False

    # -- what a worker may observe ---------------------------------------------------------

    @property
    def in_use(self) -> bool:
        """True once a worker has asked this control for anything: a live worker exists."""
        return self._live

    def touch(self) -> None:
        if self._live:
            return
        self._live = True
        try:
            self._on_live()
        except Exception:  # noqa: BLE001 -- a status line must never affect the run
            pass

    def claim_inputs(self) -> List[ControlMessage]:
        self.touch()
        try:
            texts = [m for m in self.inbox.drain(self.session_key) if m and m.strip()]
        except Exception:  # noqa: BLE001 -- polling must never break the run
            return []
        if not texts:
            return []
        out: List[ControlMessage] = []
        with self._lock:
            for text in texts:
                self._counter += 1
                message = ControlMessage(id=f"terminal-{self._counter}", text=text)
                self._unconfirmed[message.id] = text
                self._taken_texts.append(text)
                out.append(message)
        try:
            remaining = len(self.inbox.peek(self.session_key))
            self._on_sent(texts, remaining)
        except Exception:  # noqa: BLE001
            pass
        return out

    def mark_seen(self, ids: List[str], where: str) -> None:
        with self._lock:
            seen = [self._unconfirmed.pop(i) for i in ids if i in self._unconfirmed]
        if seen:
            try:
                self._on_seen(seen)
            except Exception:  # noqa: BLE001
                pass

    def cancel_requested(self) -> bool:
        self.touch()
        return bool(self._cancel_check())

    def pause_requested(self) -> bool:
        self.touch()
        return self.pause_event.is_set()

    def run_control(self) -> RunControl:
        return RunControl(
            claim_inputs=self.claim_inputs, mark_seen=self.mark_seen,
            cancel_requested=self.cancel_requested, pause_requested=self.pause_requested,
            poll_seconds=self.poll_seconds, label="terminal")

    # -- when the turn is over -------------------------------------------------------------

    def unconfirmed(self, reported: Iterable[str] = ()) -> List[str]:
        """Messages a worker took whose arrival at the model was never confirmed, in the order
        they were typed. ``reported`` is what the workers themselves returned as unseen
        (``DeepResult.unseen_inputs``): a text this control never handed out is added once."""
        with self._lock:
            out = [text for _id, text in sorted(
                self._unconfirmed.items(), key=lambda kv: int(kv[0].rsplit("-", 1)[1]))]
            known = list(self._taken_texts)
        for text in reported:
            if text and text.strip() and text not in known and text not in out:
                out.append(text)
        return out

    def put_back(self, texts: List[str]) -> None:
        """Return ``texts`` to the FRONT of the inbox, ahead of anything typed since."""
        if not texts:
            return
        rest = self.inbox.drain(self.session_key)
        for text in list(texts) + list(rest):
            self.inbox.push(self.session_key, text)


class ControlMixin:
    """The terminal side of steer, stop and pause: commands and visible delivery."""

    def _new_terminal_control(self) -> Optional[TerminalRunControl]:
        """Build this turn's control, or None when the session has no input inbox."""
        orch = getattr(self.sess, "_orch", None) if self.sess is not None else None
        inbox = getattr(orch, "input_inbox", None)
        if inbox is None:
            return None

        def on_sent(texts: List[str], remaining: int) -> None:
            self.call_from_thread(self._note_sent_to_worker, texts, remaining)

        def on_seen(texts: List[str]) -> None:
            self.call_from_thread(self._note_worker_received, texts)

        def on_live() -> None:
            self.call_from_thread(self._note_worker_live)

        return TerminalRunControl(
            inbox, self._session_id, cancel_check=self._cancel.is_set,
            on_sent=on_sent, on_seen=on_seen, on_live=on_live)

    # -- transcript lines (UI thread) -----------------------------------------------------

    def _note_worker_live(self) -> None:
        self._tlog.write(Text(
            "  Live worker: type to steer it, /pause to pause it, Esc to stop it.",
            style="dim"))

    def _note_sent_to_worker(self, texts: List[str], remaining: int) -> None:
        n = len(texts)
        self._tlog.write(Text(
            f"  ↓ {n} queued message{'s' if n != 1 else ''} sent to the running worker",
            style="dim yellow"))
        self._activity.set_queued(remaining)
        self.query_one("#prompt", PromptTextArea).placeholder = _busy_placeholder(remaining)

    def _note_worker_received(self, texts: List[str]) -> None:
        n = len(texts)
        self._tlog.write(Text(
            f"  ✓ The worker received {n} message{'s' if n != 1 else ''}", style="dim green"))

    # -- /pause and /resume ---------------------------------------------------------------

    def _cmd_pause(self) -> None:
        c = self._console
        if not self._turn_active:
            c.dim("  Nothing is running to pause.")
            return
        control = self._run_control
        if control is None or not control.in_use:
            c.dim("  No live deep worker to pause right now. Pause needs the stream_json runner "
                  "(see docs/stream-json-runner.md). Esc stops the turn.")
            return
        if control.pause_event.is_set():
            c.dim("  Already pausing.")
            return
        control.pause_event.set()
        self._activity.set_status("Pausing…")
        c.dim("  Pausing: the running step is interrupted, finished work is kept. "
              "/resume continues the same session.")

    def _cmd_resume(self) -> None:
        c = self._console
        if self._turn_active:
            c.dim("  A turn is still running. Press Esc to stop it, then /resume.")
            return
        paused = self._resumable
        if paused is None:
            c.dim("  Nothing to resume: no paused or stopped deep run in this session.")
            return
        self._resumable = None
        held = list(self._held_for_resume)
        self._held_for_resume = []
        request = paused["request"]
        parts = [f"{request}\n\nThis work was {paused['how']} and is being resumed in the same "
                 "session. Continue it from where it stopped."]
        if held:
            parts.append("Messages sent while it was not running:\n" + "\n".join(f"- {m}" for m in held))
        short = request if len(request) <= 80 else request[:79] + "…"
        self._tlog.write(Text(f"❯ /resume  ({short})", style="bold cyan"))
        if held:
            self._tlog.write(Text(
                f"  ↑ Sending {len(held)} held message{'s' if len(held) != 1 else ''} with it",
                style="bold yellow"))
        self._tlog.write(Text(""))
        self._begin_turn("\n\n".join(parts), echo=False, resume_session_id=paused["session_id"])

    # -- the end of a turn (UI thread) ----------------------------------------------------

    def _settle_run_control(self, final, cancelled: bool, error) -> str:
        """Close out this turn's control. Returns "paused", "stopped" or "".

        Whatever a live worker took but never confirmed goes back to the front of the inbox, so
        the end-of-turn flush sends it again (nothing the person typed is lost). A paused run
        keeps everything still queued for ``/resume`` instead of starting a new turn with it.
        "paused" and "stopped" are read from what the worker reported (``DeepResult.paused``,
        its session id), never guessed from the commands typed.
        """
        control = self._run_control
        if control is None:
            return ""
        deep = list(getattr(final, "deep_results", None) or [])
        reported = [t for d in deep for t in (getattr(d, "unseen_inputs", None) or [])]
        leftover = control.unconfirmed(reported)
        session_id = ""
        for d in reversed(deep):
            session_id = (getattr(d, "session_id", None) or "").strip()
            if session_id:
                break
        # A pause with no session to continue cannot be resumed, so it is handled as a stop.
        paused = bool(session_id) and any(getattr(d, "paused", False) for d in deep)
        try:
            if paused:
                held = leftover + control.inbox.drain(control.session_key)
                self._held_for_resume = [m for m in held if m and m.strip()]
                if self._held_for_resume:
                    n = len(self._held_for_resume)
                    self._tlog.write(Text(
                        f"  {n} message{'s' if n != 1 else ''} held for /resume",
                        style="dim yellow"))
            elif leftover:
                control.put_back(leftover)
                n = len(leftover)
                self._tlog.write(Text(
                    f"  ↻ {n} message{'s were' if n != 1 else ' was'} not confirmed by the "
                    "worker, so will be sent again", style="dim yellow"))
        except Exception:  # noqa: BLE001 -- bookkeeping must never break the turn's wrap-up
            pass
        if paused or (cancelled and control.in_use and session_id and error is None):
            how = "paused" if paused else "stopped"
            goals = getattr(final, "goals", None) or []
            self._resumable = {"session_id": session_id, "how": how,
                               "request": self._turn_request or "; ".join(goals)}
            return how
        return ""
