"""The conversation of this package drawn by the terminal the hub selected (the stock opencode terminal client), adapted to it.

The hub owns the screen: ``hubofhubs.api.host_in_opencode`` serves a session over the loopback interface and launches the installed client on this terminal. This module supplies only what is
this package's own, through that entry point's arguments, and mirrors none of the hub's wiring:

* the harness (``QuestSessionHarness``): one turn of the attended conversation is one run. It grounds the turn in the quest it is about (the same ``InteractiveSession.turn_grounding`` the
  Textual screen uses), runs the orchestrator's stream, turns its progress events into the hub's session events, and keeps the turn through ``record_turn`` (the Textual screen's own
  bookkeeping), so a conversation drawn here resumes from the Textual screen and the other way round;
* the header lines beside who the AI acts for (the matched or selected quest, the persona, the model hint, the line of saved notes the turn looked at);
* the slash commands (``QuestCommands``): the session's own command handlers, their output captured as one quiet row each;
* the model picker: the hub's tiers, then the models it serves (``offered_choices``), with ``auto`` first (the orchestrator decides).

What the opencode client cannot show (a numbered picker for a quest or a persona, ``/pause``, the sources panel) is a command that lists and takes a name instead, or is left to the Textual screen.
Choose the screen with ``quest-ai-runner chat --screen opencode`` or ``QAR_SCREEN``; ``chat`` keeps the Textual screen as the fallback.
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from esoi import implementation

from quest_ai_runner.the_consciousness.terminal import terminal_words  # noqa: F401  (registers the words this package uses in the hub's blocks)
from quest_ai_runner.the_consciousness.terminal.command_menu import parse_help, quest_items
from quest_ai_runner.the_consciousness.terminal.interactive_session_persist import record_stopped_turn, record_turn
from quest_ai_runner.the_consciousness.terminal.interactive_session_render import _Console
from quest_ai_runner.the_consciousness.terminal.interactive_session_text import _HELP

if TYPE_CHECKING:
    from quest_ai_runner.the_body.configuration.config import RunnerConfig

AUTO = "auto"                         # the picker's first choice: the orchestrator decides the tier (no model hint)
COMMAND_PREFIX = "quest-ai-runner chat"
HEARTBEAT_PHASE = "heartbeat"
WAIT_ROW_AFTER_SECONDS = 3.0      # a stretch with no event this long gets a row naming what the turn is on
WAIT_TICK_SECONDS = 1.0
# The commands this screen can run. The others of /help are the Textual screen's own (its menus, panels and keys): /quit (the client has its own exit), /models (the client's model picker), /trace,
# /updates, /continue, /notnow, /answer, /pause and /resume.
SUPPORTED = ("help", "clear", "reps", "rep", "file", "whoami", "status", "tasks", "model", "depth", "system", "replan", "save", "load", "sessions", "quest")


class CapturedConsole(_Console):
    """The session's console with the lines kept (no colour, nothing printed), so a command handler's output can be said as one row in the client's transcript."""

    def __init__(self) -> None:
        super().__init__()
        self._rich = None
        self._color = False
        self.lines: List[str] = []

    def write(self, s: str) -> None:
        self.lines.extend(s.splitlines())

    def line(self, s: str = "") -> None:
        self.lines.append(s)

    def dim(self, s: str) -> None:
        self.lines.append(s)

    def bullet(self, text: str, indent: int = 0, color: Optional[str] = None) -> None:
        self.lines.append("  " * indent + "- " + text)

    def markdown(self, text: str) -> None:
        self.lines.extend(text.splitlines())

    def speaker(self, label: str, color: str, text: str) -> None:
        self.lines.append(f"{label}  {text}".rstrip())

    def rule(self) -> None:
        return None

    def take(self) -> List[str]:
        out, self.lines = self.lines, []
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        return out


class Owner:
    """The attended session, built off the start path (the client opens at once; the first turn or command waits for it), and the state the header and the commands share."""

    def __init__(self, config: "RunnerConfig", *, rep_name: str, persona: Optional[str], goal_id: Optional[str], rep_specified: bool, persona_specified: bool, resume: Any = None,
                 verbosity: int = 0, factory: Optional[Callable[[], Any]] = None) -> None:
        self.config, self.resume = config, resume
        self.console = CapturedConsole()
        self.notes_line = ""                   # "Looked at 3 sources: ..." of the latest turn
        self.say: Callable[[Any], None] = lambda lines: None      # set to the bridge's ``say`` once the client's session exists
        self.select_picker: Callable[[str], bool] = lambda tier: False
        self.error: Optional[BaseException] = None
        self.ready = threading.Event()
        self.announced = False                  # notices that arrive before the announcement are in the session's list and said with it
        self._session: Any = None
        self._factory = factory or (lambda: self.build(rep_name, persona, goal_id, rep_specified, persona_specified, verbosity))
        threading.Thread(target=self.start_session, name="qar-opencode-session", daemon=True).start()

    def build(self, rep_name: str, persona: Optional[str], goal_id: Optional[str], rep_specified: bool, persona_specified: bool, verbosity: int) -> Any:
        from quest_ai_runner.the_consciousness.terminal.interactive_session import InteractiveSession
        return InteractiveSession(self.config, rep_name=rep_name, persona=persona, goal_id=goal_id, verbose=bool(verbosity), rep_specified=rep_specified,
                                  persona_specified=persona_specified, resume=self.resume, _startup_notify=self.notice)

    @staticmethod
    def is_progress(message: str) -> bool:
        """A startup line that only says something is under way (it ends with an ellipsis): the transcript is permanent, so the line that states the outcome is the one worth a row."""
        return message.rstrip().endswith(("...", "\u2026"))

    def notice(self, message: str) -> None:
        """A startup notice that arrives after the client is up (the context index finishing, a tidy): one quiet row, unless it is only progress."""
        if self.announced and not self.is_progress(message):
            self.say([message])

    def start_session(self) -> None:
        try:
            session = self._factory()
            session._console = self.console
            self._session = session
        except BaseException as error:  # noqa: BLE001 -- said in the client's transcript at the first turn or command, never a traceback on the person's terminal
            self.error = error
        finally:
            self.ready.set()

    def session(self, timeout: float = 120.0) -> Any:
        if not self.ready.wait(timeout):
            raise RuntimeError("the session is still starting; try again in a moment")
        if self._session is None:
            raise RuntimeError(f"the session could not start: {self.error}")
        return self._session

    # --- what the header says -------------------------------------------------------------------------------------------------

    def header(self) -> List[str]:
        s = self._session
        if s is None:
            return ["Starting the session..."]
        lines: List[str] = [self.ai_line()]
        pinned, matched = getattr(s, "pinned_quest", None), getattr(s, "turn_quest", None)
        if pinned is not None:
            lines.append(f"Quest: {pinned.title or pinned.quest_id} (selected)")
        elif getattr(s, "quest_match_mode", "auto") == "none":
            lines.append("Quest: none")
        elif matched is not None:
            lines.append(f"Quest: {matched.title or matched.quest_id} (matched)")
        else:
            lines.append("Quest: matched per message")
        # The AI line already names the rep; the persona is a second line only when it says something else (never "AI: Joshua" over "Persona: joshua").
        persona = (os.path.basename(os.path.dirname(s._persona_file)) or os.path.basename(s._persona_file)) if s._persona_file else ("loaded" if s._persona else "")
        if persona and persona.strip().lower() != str(getattr(s, "_rep_name", "") or "").strip().lower():
            lines.append(f"Persona: {persona}")
        lines.append(f"Model: {s._model_hint or AUTO}")
        if self.notes_line:
            lines.append(self.notes_line)
        return lines

    def ai_line(self) -> str:
        """Which AI answers, in words a person understands: the rep's name. A session always has one; with nothing chosen it is the built-in General assistant."""
        name = str(getattr(self._session, "_rep_name", "") or "").strip()
        if not name or name == "Assistant":
            return "AI: General assistant"
        return f"AI: {name}"

    def person(self) -> str:
        """Nothing: the AI line of ``header`` says who answers, and the client's own "acting for" label means a person the AI works for."""
        return ""


class QuestSessionHarness:
    """One turn of the attended conversation as a hub run (the contract ``Harness``): events out through the sink, stop and steer in through ``control``."""
    name = "quest-ai-runner"

    def __init__(self, owner: Owner) -> None:
        self.owner = owner
        self.cancel = threading.Event()
        self.sent: Dict[str, str] = {}          # a steering note's text -> the id the client gave it, so the harness can say when the worker took it up
        self.control_in_use: Optional[Any] = None
        self.inbox_key = f"opencode-{id(self)}"

    @property
    def capabilities(self) -> Any:
        from hubofhubs.api import Capabilities
        session = self.owner._session
        inbox = getattr(getattr(session, "_orch", None), "input_inbox", None) if session is not None else None
        return Capabilities(stop=True, resume=True, steer=inbox is not None, approve=False, steer_ack=True, live_agents=True)

    # --- controls ---------------------------------------------------------------------------------------------------------------

    def control(self, control: Any) -> bool:
        from hubofhubs.api import Steer, Stop
        if isinstance(control, Stop):
            self.cancel.set()
            return True
        if isinstance(control, Steer):
            inbox = getattr(getattr(self.owner._session, "_orch", None), "input_inbox", None)
            if inbox is None or not control.text.strip():
                return False
            self.sent[control.text] = control.message_id
            inbox.push(self.inbox_key, control.text)
            return True
        return False

    # --- one turn ---------------------------------------------------------------------------------------------------------------

    def start(self, goal: str, sink: Callable[[Any], None], resume_session_id: str = "") -> Any:
        from hubofhubs.api import Finished, RunStarted
        from quest_ai_runner.the_consciousness.terminal.textual_ui_outcome import provider_error_lines
        session = self.owner.session()
        self.cancel.clear()
        begun = time.monotonic()
        conv = self.conversation_id(session)
        sink(RunStarted(goal=goal, harness=self.name, session_id=conv, resumed=bool(resume_session_id and session._turn_count)))
        turn = Turn(self, session, sink, begun)
        try:
            final = turn.run(goal)
        except Exception as error:  # noqa: BLE001 -- a failed model call is said in plain words, the session goes on
            report = "\n".join(provider_error_lines(error, message_restored=False))
            done = Finished(outcome="failed", report=report, error=str(error)[:300], session_id=conv, at=time.monotonic() - begun, elapsed_seconds=time.monotonic() - begun)
            sink(done)
            return done
        done = turn.finish(goal, final, conv, begun)
        sink(done)
        return done

    @staticmethod
    def conversation_id(session: Any) -> str:
        """The short id ``chat --resume`` takes (the saved conversation's, without its file prefix); empty before anything is saved."""
        from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.chat_conversations import FILE_PREFIX
        conv = str(getattr(session, "_conv_id", "") or "")
        return conv[len(FILE_PREFIX):] if conv.startswith(FILE_PREFIX) else conv


class Turn:
    """The state of one turn: the events of the orchestrator's stream as the hub's session events, and what the closing needs."""

    def __init__(self, harness: QuestSessionHarness, session: Any, sink: Callable[[Any], None], begun: float) -> None:
        from hubofhubs.api import StreamJson
        self.harness, self.session, self.sink, self.begun = harness, session, sink, begun
        self.stream = StreamJson()
        self.parts: List[str] = []             # the answer as it streamed
        self.partial_started = False
        self.question = ""
        self.last_narration = ""
        self.runs: Dict[str, bool] = {}        # exec run id -> its step is open
        self.cancelled = False
        self.control: Optional[Any] = None
        self.last_event = time.monotonic()       # when the stream last said anything: a wait longer than WAIT_ROW_AFTER_SECONDS gets a row that names what it is waiting on
        self.status_text = ""
        self.wait_open = ""                       # the id of the open "still working" row, empty when none
        self.waits = 0
        self.wait_lock = threading.Lock()

    def at(self) -> float:
        return time.monotonic() - self.begun

    def say(self, text: str) -> None:
        if text.strip():
            self.harness.owner.say(text)

    def run(self, goal: str) -> Optional[Any]:
        """Ground the turn, run the orchestrator's stream and give each event to ``take``; the final result, or None when the person stopped it first."""
        from quest_ai_runner.the_consciousness.terminal.textual_ui_control import TerminalRunControl
        from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import describe_match
        from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult
        s, cancel = self.session, self.harness.cancel
        model_hint = s._model_hint
        if s._replan_next:
            model_hint, s._replan_next = "quality", False
            self.say("Replan mode: using the quality tier for this turn.")
        preamble, grounding = s.turn_grounding(goal)
        if s.turn_quest is not None:
            self.say(f"{'Selected' if s.turn_quest is s.pinned_quest else 'Matched'} {describe_match(s.turn_quest)}")
        inbox = getattr(s._orch, "input_inbox", None)
        key = self.harness.inbox_key
        extra: Dict[str, Any] = {}
        pending = None
        if inbox is not None:
            self.control = TerminalRunControl(inbox, key, cancel_check=cancel.is_set, on_seen=self.taken_up)
            extra["run_control"] = self.control.run_control()
            pending = lambda: inbox.drain(key)  # noqa: E731
        stream = s._orch.run_stream(goal, transcript=s._last_transcript(), quest_id=s.bound_quest_id(), rep_preamble=preamble, model_hint=model_hint, pending_inputs=pending,
                                    conv_id=s._conv_id, context_meta=grounding, cancel_check=cancel.is_set, **extra)
        final = None
        ticker_done = threading.Event()
        threading.Thread(target=self.watch_waits, args=(ticker_done,), name="qar-opencode-waits", daemon=True).start()
        try:
            for item in stream:
                if cancel.is_set():
                    if self.control is None or not self.control.in_use:
                        break
                    if isinstance(item, OrchestratorResult):      # a live worker is being stopped: let it finish, so its result is kept
                        final = item
                    continue
                if isinstance(item, OrchestratorResult):
                    final = item
                else:
                    self.take(item)
        finally:
            ticker_done.set()
            self.close_wait()
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        self.cancelled = cancel.is_set()
        self.close_runs()
        self.drop_unconfirmed(inbox, key)
        return final

    # --- the orchestrator's events ------------------------------------------------------------------------------------------------

    def take(self, raw: Any) -> None:
        """One progress event: the hub's own adapters read what they know (the contract's events, a worker's stream lines, a limit), the rest is worded here as one quiet row or a step."""
        from hubofhubs.api import (EVENT_CONTEXT, EVENT_DECISION, EVENT_EXEC, EVENT_INTENT, EVENT_PARTIAL, EVENT_REPLAN, EVENT_RESULT, EVENT_STATUS, EVENT_UNDERSTANDING, Step,
                                   progress_events)
        event = as_event(raw)
        kind, text, data = event.type, event.text, event.data
        self.last_event = time.monotonic()
        self.close_wait()
        if kind == EVENT_STATUS and text.strip() and not (isinstance(data, dict) and "deep_run_model" in data):
            self.status_text = text.strip()
        for item in progress_events(event, stream=self.stream):
            if getattr(item, "type", "") != "waiting":       # a question of this package is answered by the next line typed, not by approve or refuse (``question`` below)
                self.sink(with_time(item, self.at()))
        if kind == EVENT_PARTIAL:
            if data.get("narration") or data.get("ack"):
                beat = text.strip()
                if beat and beat != self.last_narration:
                    self.last_narration = beat
                    self.sink(Step(at=self.at(), step_id=f"narration-{len(self.runs)}-{abs(hash(beat)) % 10**6}", kind="note", label=beat, state="done"))
                return
            self.partial_started = True
            self.parts.append(text)
        elif kind == EVENT_STATUS:
            if isinstance(data, dict) and "deep_run_model" in data:
                return
            if isinstance(data, dict) and "deep_run_model_live" in data or text.startswith("Starting"):
                self.say("▸ " + text.rstrip("…"))
        elif kind == EVENT_CONTEXT:
            from quest_ai_runner.the_consciousness.terminal.textual_ui_helpers import context_block_lines
            lines = [part.plain.strip() for part in context_block_lines(data.get("card_metadata") or [], data.get("sources") or [])]
            if lines:
                self.harness.owner.notes_line = lines[0]
                self.say(lines[0])
        elif kind == EVENT_UNDERSTANDING and text:
            self.say("◆ " + text)
        elif kind == EVENT_INTENT and text:
            self.say("▸ " + text)
        elif kind == EVENT_REPLAN and text:
            self.say("↺ replan  " + text)
        elif kind == EVENT_EXEC:
            self.exec_event(data)
        elif kind == EVENT_RESULT:
            if not self.partial_started and text:
                self.parts.append(text)
        elif kind == EVENT_DECISION and text:
            self.question = text

    def watch_waits(self, done: threading.Event) -> None:
        """A wait the person would otherwise sit through in silence: after WAIT_ROW_AFTER_SECONDS with no event, one running row says what the turn is on (the last status it
        gave), and the next event closes it. The row is true (it names the stage the stream last reported) and never a clock of its own: the client shows how long the row has run."""
        while not done.wait(WAIT_TICK_SECONDS):
            if time.monotonic() - self.last_event < WAIT_ROW_AFTER_SECONDS:
                continue
            with self.wait_lock:
                if self.wait_open or done.is_set():
                    continue
                from hubofhubs.api import Step
                self.waits += 1
                self.wait_open = f"wait-{self.waits}"
                label = self.status_text or "Working on your request…"
                self.sink(Step(at=self.at(), step_id=self.wait_open, kind="status", label=label, state="running"))

    def close_wait(self) -> None:
        with self.wait_lock:
            if not self.wait_open:
                return
            from hubofhubs.api import Step
            step_id, self.wait_open = self.wait_open, ""
            self.sink(Step(at=self.at(), step_id=step_id, kind="status", label=self.status_text or "Working on your request…", state="done"))

    def exec_event(self, data: Dict[str, Any]) -> None:
        """A worker's first sign of life is one running row for its goal (a worker that reports the contract's own events has its steps drawn from them); the row ends with the turn."""
        from hubofhubs.api import Step
        run_id = str(data.get("run_id") or "default")
        if run_id in self.runs or data.get("phase") == HEARTBEAT_PHASE:
            return
        goal = " ".join(str(data.get("goal") or "Working").split())
        self.runs[run_id] = True
        self.sink(Step(at=self.at(), step_id=f"run-{run_id}", kind="note", label=goal[:120], state="running"))

    def close_runs(self) -> None:
        from hubofhubs.api import Step
        for run_id, open_ in self.runs.items():
            if open_:
                self.sink(Step(at=self.at(), step_id=f"run-{run_id}", kind="note", label="", state="done"))
                self.runs[run_id] = False

    def taken_up(self, texts: List[str]) -> None:
        """The live worker confirmed it has the person's notes: the hub says each as taken up."""
        from hubofhubs.api import Step
        for text in texts:
            self.sink(Step(at=self.at(), step_id=self.harness.sent.get(text, ""), kind="message-received", label=text, state="done"))

    def drop_unconfirmed(self, inbox: Any, key: str) -> None:
        """What was typed during the turn and the worker never confirmed stays with the bridge (it starts it as the next run); the copy in this session's inbox is dropped so it is never taken twice."""
        if inbox is None:
            return
        try:
            if self.control is not None:
                self.control.unconfirmed()
            inbox.drain(key)
        except Exception:  # noqa: BLE001 -- bookkeeping never breaks the turn's wrap-up
            pass

    # --- the end of the turn ------------------------------------------------------------------------------------------------------

    def finish(self, goal: str, final: Optional[Any], conv: str, begun: float) -> Any:
        """Keep the turn (the history and the saved conversation, as the Textual screen does) and give the closing: how it ended, the answer and anything the person must read."""
        from hubofhubs.api import Finished
        from quest_ai_runner.the_consciousness.terminal.textual_ui_outcome import (
            OUTCOME_FINISHED, OUTCOME_LIMIT, OUTCOME_WAITING, deep_results, launch_failure_lines, limit_lines, limit_reset, result_items, turn_outcome)
        s = self.session
        elapsed = time.monotonic() - begun
        conv = QuestSessionHarness.conversation_id(s) or conv
        if final is None or self.cancelled:
            record_stopped_turn(s, goal)
            return Finished(outcome="stopped", report="", session_id=conv, at=elapsed, elapsed_seconds=elapsed)
        launch_lines = launch_failure_lines(final)
        outcome = turn_outcome(final)
        answer = self.answer_of(final)
        record_turn(s, goal, final, answer=answer, fallback_text="\n".join(self.parts).strip(), could_not_start=bool(launch_lines))
        extra: List[str] = []
        if launch_lines:
            extra += launch_lines
        if outcome == OUTCOME_LIMIT:
            extra += limit_lines(limit_reset(final))
        question = (self.question or getattr(final, "question", None) or "").strip()
        if outcome == OUTCOME_WAITING and question:
            extra.append("? " + question + "\n\nType your answer as your next message.")
        report = "\n\n".join(part for part in [answer or "", *extra] if part and part.strip())
        results = deep_results(final)
        cost = sum(float(getattr(r, "cost_usd", 0.0) or 0.0) for r in results) or None
        changed = tuple(item[len("changed "):] for r in results for item in (getattr(r, "observations", None) or []) if str(item).startswith("changed "))
        met = outcome in (None, OUTCOME_FINISHED)
        from quest_ai_runner.the_consciousness.terminal.turn_usage import turn_usage
        used = turn_usage(final, getattr(s._orch, "provider", None), Finished)       # the whole turn's tokens and calls, and "included in subscription" on a flat plan
        return Finished(outcome="met" if met else "unmet", report=report, changed=changed, session_id=conv, cost_usd=cost, at=elapsed, elapsed_seconds=elapsed,
                        spend_usd=cost, end_reason="" if met else (outcome or ""), **used)

    def answer_of(self, final: Any) -> Optional[str]:
        """The reply the person reads: the result's text, else what streamed, else (a deep turn) what its workers produced."""
        from quest_ai_runner.the_consciousness.terminal.textual_ui_outcome import deep_results
        text = (final.text or "\n".join(self.parts).strip() or "").strip()
        if text:
            return text
        outputs = [str(getattr(r, "output", "") or "").strip() for r in deep_results(final)]
        return "\n\n".join(o for o in outputs if o) or None


def as_event(raw: Any) -> Any:
    """A progress event as an object with ``type``, ``text``, ``data`` (the orchestrator yields both typed events and dicts)."""
    if isinstance(raw, dict):
        return SimpleNamespace(type=raw.get("type", ""), text=(raw.get("text") or "").rstrip(), data=raw.get("data") or {}, action=raw.get("action") or "", result_kind=raw.get("result_kind") or "",
                               decision_id=raw.get("decision_id"))
    return SimpleNamespace(type=getattr(raw, "type", ""), text=(getattr(raw, "text", None) or "").rstrip(), data=getattr(raw, "data", None) or {}, action=getattr(raw, "action", None) or "",
                           result_kind=getattr(raw, "result_kind", None) or "", decision_id=getattr(raw, "decision_id", None))


def with_time(event: Any, at: float) -> Any:
    """The event with the clock of this turn when the adapter left it at zero."""
    import dataclasses
    if getattr(event, "at", 1) == 0.0 and dataclasses.is_dataclass(event):
        return dataclasses.replace(event, at=at)
    return event


class QuestCommands:
    """The session's own command handlers as the client's slash commands: each runs the handler with the console captured and says the lines as one row."""

    def __init__(self, owner: Owner) -> None:
        self.owner = owner
        self.lock = threading.Lock()

    def specs(self) -> List[Any]:
        from hubofhubs.api import SlashCommand
        described = {item.label.split(", ")[0].split(" ")[0].lstrip("/"): item.description for item in parse_help(_HELP)}
        return [SlashCommand(name, described.get(name, name), self.runner(name)) for name in SUPPORTED]

    def runner(self, name: str) -> Callable[[str], List[str]]:
        def run(argument: str) -> List[str]:
            session = self.owner.session()
            with self.lock:
                console = self.owner.console
                console.take()
                getattr(self, "do_" + name)(session, argument)
                return console.take()
        return run

    # --- the commands -----------------------------------------------------------------------------------------------------------

    def do_help(self, s: Any, arg: str) -> None:
        for item in parse_help(_HELP):
            if item.label.split(", ")[0].split(" ")[0].lstrip("/") in SUPPORTED:
                s._console.line(f"{item.label}  {item.description}")
        s._console.line("The model is chosen in the client's own model picker: auto, the hub's tiers, then the models it serves.")

    def do_clear(self, s: Any, arg: str) -> None:
        s._last_user = s._last_assistant = ""
        s._session_history.clear()      # in place: the conversation store resolving "it" and "that one" holds this same list
        s._console.dim("Started fresh: the AI no longer sees this conversation's earlier turns.")

    def do_reps(self, s: Any, arg: str) -> None:
        from quest_ai_runner.the_consciousness.terminal.interactive_session_persona import discover_reps
        skills = s._skills_dir()
        reps = discover_reps(skills)
        if not reps:
            s._console.dim(f"No SKILL.md files found in {skills}. Use /rep <name> and /file <path>.")
            return
        s._console.dim("Representatives (choose one with /rep <name>):")
        for rep in reps:
            s._console.dim(f"  {rep['display_name']}" + ("  (current)" if rep["display_name"] == s._rep_name else ""))

    def do_rep(self, s: Any, arg: str) -> None:
        from quest_ai_runner.the_consciousness.terminal.interactive_session_persona import discover_reps
        name = arg.strip()
        if not name:
            s._console.dim(f"Representative: {s._rep_name}. Usage: /rep <name>, or /reps to list them.")
            return
        found = next((r for r in discover_reps(s._skills_dir()) if name.lower() in (r["name"].lower(), r["display_name"].lower())), None)
        s._rep_name = found["display_name"] if found else name
        if found:
            try:
                s._persona = open(found["skill_file"]).read()
                s._persona_file = found["skill_file"]
            except OSError as error:
                s._console.dim(f"Could not read {found['skill_file']!r}: {error}")
        s._console.dim(f"Representative: {s._rep_name}" + (f"  (loaded {found['skill_file']})" if found else ""))
        s._persist_session_state()

    def do_file(self, s: Any, arg: str) -> None:
        path = arg.strip()
        if not path:
            s._console.dim("Usage: /file <path> loads a persona file.")
            return
        try:
            s._persona = open(path).read()
            s._persona_file = path
            s._console.dim(f"Loaded: {path} ({max(1, len(s._persona.encode()) // 1024)}KB)")
            s._persist_session_state()
        except OSError as error:
            s._console.dim(f"Could not read {path!r}: {error}")

    def do_whoami(self, s: Any, arg: str) -> None:
        s._print_whoami()

    def do_status(self, s: Any, arg: str) -> None:
        s._print_status()

    def do_tasks(self, s: Any, arg: str) -> None:
        s._print_tasks()

    def do_model(self, s: Any, arg: str) -> None:
        s._cmd_model(arg)
        self.follow_model(s)

    def do_depth(self, s: Any, arg: str) -> None:
        s._cmd_depth(arg)
        self.follow_model(s)

    def follow_model(self, s: Any) -> None:
        """The client's picker shows the choice the person made by command."""
        self.owner.select_picker(s._model_hint or AUTO)

    def do_system(self, s: Any, arg: str) -> None:
        s._cmd_system(arg)

    def do_replan(self, s: Any, arg: str) -> None:
        s._replan_next = True
        s._console.dim("Next turn will use the quality tier for a fresh re-planning pass.")

    def do_save(self, s: Any, arg: str) -> None:
        s._cmd_save(arg)

    def do_load(self, s: Any, arg: str) -> None:
        if not arg.strip():
            s._console.dim("Usage: /load <name>; /sessions lists the saved sessions.")
            return
        s._cmd_load(arg)

    def do_sessions(self, s: Any, arg: str) -> None:
        s._cmd_sessions()

    def do_quest(self, s: Any, arg: str) -> None:
        """The quest picker: no argument lists the quests this account can reach (a name or an id after the command selects one); auto and none are the matching modes."""
        if arg.strip():
            s.cmd_quest(arg)
            return
        pinned = getattr(s, "pinned_quest", None)
        s.cmd_quest("")
        choices = quest_items("", s.quest_choices(), getattr(s, "quest_match_mode", "auto"), pinned.quest_id if pinned is not None else None)
        if choices:
            s._console.dim("Choose one with /quest <name or id>:")
            for item in choices:
                s._console.dim(f"  {item.label}  ·  {item.description}")


@implementation("terminal-session", "opencode_terminal")
class OpencodeTerminal:
    """This package's conversation drawn by the terminal the hub selected (the stock opencode client), with the quest, the persona, the saved-notes line and the slash commands."""

    @staticmethod
    def unavailable(client: str = "", environ: Optional[Dict[str, str]] = None) -> str:
        """Why the client cannot run here (with the install hint), or an empty string."""
        from hubofhubs.api import opencode_available
        return opencode_available(client, environ)

    def start(self, config: "RunnerConfig", *, rep_name: str = "Assistant", persona: Optional[str] = None, goal_id: Optional[str] = None, rep_specified: bool = True,
              persona_specified: bool = True, resume: Any = None, verbosity: int = 0, client: str = "", mode: str = "", folder: str = ".",
              environ: Optional[Dict[str, str]] = None, say: Callable[[str], None] = print) -> Optional[str]:
        """Open the client on this terminal and return the id of the conversation the session wrote to (for the resume hint), or None when nothing was saved."""
        from hubofhubs.api import default_tier, host_in_opencode, offered_choices
        from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.chat_conversations import chat_log_path
        owner = Owner(config, rep_name=rep_name, persona=persona, goal_id=goal_id, rep_specified=rep_specified, persona_specified=persona_specified, resume=resume, verbosity=verbosity)
        harness = QuestSessionHarness(owner)
        tiers = (AUTO,) + tuple(offered_choices())

        def build(where: str, tier: str = "") -> QuestSessionHarness:
            """The tier the person picked in the client applies to the next turn, as the model hint (``auto`` is none)."""
            session = owner.session()
            if tier:
                session._model_hint = None if tier == AUTO else (session._model_choice(tier) or tier)
            return harness

        def keep(bridge: Any) -> None:
            owner.say = bridge.say
            owner.select_picker = bridge.select_tier
            threading.Thread(target=self.announce, args=(owner, bridge), name="qar-opencode-announce", daemon=True).start()

        try:
            host_in_opencode(build, folder, client=client, mode=mode, environ=environ, person="",  # the header's AI line says who answers; "acting for" is a person the AI works for
                            command=self.resume_prefix(rep_name if rep_specified else None), tiers=tiers,
                             tier=AUTO, commands=QuestCommands(owner).specs(), header=owner.header, say=say, color=bool(os.isatty(1)), on_bridge=keep, log_path=str(chat_log_path()))
        except KeyboardInterrupt:      # Ctrl+C is the person leaving: no traceback; the hub already said how to continue
            pass
        session = owner._session
        from quest_ai_runner.the_consciousness.terminal.textual_session import saved_conversation_id
        return saved_conversation_id(SimpleNamespace(sess=session), resume) if session is not None else None

    @staticmethod
    def resume_prefix(rep: Optional[str]) -> str:
        """How the resume command starts (the hub adds ``--resume <id>``): the Textual screen's own words, with this screen named so the same conversation opens here."""
        return " ".join([COMMAND_PREFIX] + ([rep] if rep else []) + ["--screen opencode"])

    @staticmethod
    def announce(owner: Owner, bridge: Any) -> None:
        """When the session is built: the startup notices it collected, and a line saying a resumed conversation's turns are the AI's context."""
        try:
            session = owner.session()
        except RuntimeError as error:
            bridge.say(str(error))
            return
        owner.announced = True
        lines = [line for line in (getattr(session, "_startup_notices", None) or []) if not owner.is_progress(line)]
        if getattr(session, "resumed", None) is not None:
            lines.append(f"Continuing a saved conversation of {len(session._session_history)} turns; the AI has them as context.")
        bridge.say(lines)


__all__ = ["OpencodeTerminal", "Owner", "QuestCommands", "QuestSessionHarness", "Turn", "CapturedConsole", "AUTO"]
