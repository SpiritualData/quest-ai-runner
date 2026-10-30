"""App startup: session build worker, header and banner, resumed-history replay, terminal title.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from rich.text import Text

from .interactive_session import InteractiveSession, _BANNER, _BOLD, _CYAN, _DIM, _RESET
from .textual_ui_helpers import _missing_model_provider_message, indented_line, message_count, person_line, plural
from .textual_ui_outcome import speaker_label, work_lines


class StartupMixin:
    """App startup: session build worker, header and banner, resumed-history replay, terminal title."""

    RESUME_REPLAY_TURNS = 10

    def replay_resumed_history(self, conv, rep_name: str) -> None:
        """Show the tail of a `chat --resume` conversation so the user sees where they left off.

        Only the last RESUME_REPLAY_TURNS turns are rendered (a long conversation would otherwise
        flood the scrollback); the session's history, which the model reads, holds all of them.
        """
        history = conv.history
        c = self._console
        # One plain line. The conversation's id is for the person debugging (``-v``), not for the one chatting.
        # Messages, not turns: each turn is the person's message and the AI's reply.
        detail = plural(message_count(history), "message") + (f", id {conv.short_id[:12]}" if self.verbosity >= 1 else "")
        c.dim(f"  Picking up your last conversation ({detail}).")
        shown = history[-self.RESUME_REPLAY_TURNS:]
        if len(history) > len(shown):
            c.dim(f"  {plural(message_count(history[:len(history) - len(shown)]), 'earlier message')} not shown.")
        c.line("")
        saved_shown = conv.meta.get("shown") if isinstance(conv.meta.get("shown"), list) else []
        first = len(history) - len(shown)
        for offset, (user_text, asst_text) in enumerate(shown):
            self._tlog.write(person_line(user_text))
            self._tlog.write(Text(""))
            index = first + offset
            on_screen = saved_shown[index] if index < len(saved_shown) and isinstance(saved_shown[index], str) else ""
            asst_text = on_screen or asst_text      # what the turn showed, else the note the model reads back
            saved_work = conv.meta.get("work") if isinstance(conv.meta.get("work"), list) else []
            for line in work_lines(saved_work[index] if index < len(saved_work) else []):
                self._tlog.write(indented_line(f"  {line}", "dim"))       # the tasks that ran, and on which model
            if asst_text:
                self._tlog.write(Text(speaker_label(rep_name), style="bold cyan"))
                self._console.markdown(asst_text)
            self._tlog.write(Text(""))

    def _build_session_worker(self) -> None:
        """Worker thread: build InteractiveSession (loads embedder, Qdrant, etc.) then hand off."""
        try:
            missing = _missing_model_provider_message()
            if missing is not None:
                self.call_from_thread(self._startup_failed, RuntimeError(missing))
                return

            from .interactive_session import InteractiveSession

            # Use a flag so we can stop forwarding notices the moment the session
            # is returned — background threads (e.g. the bootstrap indexer) fire
            # after that point and should not appear in the transcript.
            _active = [True]

            def _live_notice(msg: str) -> None:
                if _active[0]:
                    self.call_from_thread(setattr, self, "sub_title", msg)

            session = InteractiveSession(
                self._deferred_config,
                rep_name=self._deferred_rep_name,
                persona=self._deferred_persona,
                goal_id=self._deferred_goal_id,
                _startup_notify=_live_notice,
                verbose=self.verbosity >= 1,
                rep_specified=self._deferred_rep_specified,
                persona_specified=self._deferred_persona_specified,
                resume=self.deferred_resume,
            )
            _active[0] = False  # suppress any background-thread notices from here on
            self.call_from_thread(self._finish_startup, session)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._startup_failed, exc)

    def _finish_startup(self, session: InteractiveSession) -> None:
        """Called on the UI thread once the session is ready."""
        self.sub_title = ""  # clear any loading status from the header
        self.sess = session
        self.rep_name = session._rep_name
        session._console = self._console
        self._deferred_config = None
        self._deferred_rep_name = None
        self._deferred_persona = None
        self._deferred_goal_id = None
        # Banner was already shown by _print_static_banner; the resumed history too, when there is one.
        if getattr(session, "resumed", None) is not None and self.deferred_resume is None:
            # Not already shown at mount (a session built outside the deferred path).
            self.replay_resumed_history(session.resumed, session._rep_name)
        self.announce_ready(session)
        self.deferred_resume = None
        listeners = getattr(session, "quest_list_listeners", None)
        if isinstance(listeners, list):
            listeners.append(lambda: self.call_from_thread(self.pick_quest, True))
        # Replay any messages the user typed before the session was ready.
        for queued_line in self._pre_session_queue:
            if queued_line.startswith("/"):
                self._dispatch_command(queued_line)
            else:
                self._begin_turn(queued_line, echo=False)
        self._pre_session_queue.clear()

    def _startup_failed(self, exc: Exception) -> None:
        """Called on the UI thread when session init fails — show the error and quit."""
        if self._console:
            self._console.write(f"[red]Startup failed: {exc}[/red]")
        self.exit(1)

    def _print_static_banner(self) -> None:
        self._console.write(_BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM))

    def _print_header(self) -> None:
        self._console.write(_BANNER.format(B=_BOLD, C=_CYAN, R=_RESET, D=_DIM))
        self.announce_ready(self.sess)

    def announce_ready(self, session: InteractiveSession) -> None:
        """Who is answering and from which folder, then one ``Ready.``: the same two lines for a fresh start and a resumed one."""
        parts = [f"AI: {session._rep_name}"]
        folder = getattr(session._cfg, "corpus_root", None)
        if folder:
            parts.append(f"folder: {folder.split('/')[-1] if '/' in folder else folder}")
        if session._goal_id:
            parts.append(f"goal: {session._goal_id}")
        if session._model_hint:
            parts.append(f"model: {session._model_hint}")
        c = self._console
        c.dim("  " + "  •  ".join(parts))
        c.dim("  Ready.")
        c.line("")

    # -- turn lifecycle --------------------------------------------------------

    def _set_terminal_title(self, title: str) -> None:
        """Update both the Textual header and the terminal window/pane title."""
        self.title = title
        try:
            if hasattr(self, "_driver") and self._driver is not None:
                self._driver.write(f"\033]0;{title}\007")
        except Exception:
            pass
