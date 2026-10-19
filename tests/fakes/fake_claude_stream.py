"""A scripted stand-in for ``claude -p --input-format stream-json --output-format stream-json``.

It speaks the same line protocol as Claude Code 2.1.x, costs nothing and makes no model call, so the
stream-json deep runner can be tested offline. What it does:

  * ``--version`` prints a version; every other call is a session.
  * Reads ``user`` lines and ``control_request`` lines from stdin. The first user line is the
    prompt. It then runs the scenario's ``steps`` (each a tool call that takes ``seconds``).
  * A user line that arrives WHILE a tool runs waits; at the next model turn (when the tool has
    finished) it is replayed (``--replay-user-messages``) and obeyed: ``skip <step>`` drops a step
    that has not run, ``add <step>`` appends an instant step. That is the steering behaviour the
    real probe showed.
  * ``control_request`` ``interrupt`` stops the running tool, answers with a ``control_response``,
    ends the turn with ``error_during_execution`` and KEEPS the session open. With
    ``ignore_interrupt`` it does nothing, which is the worker the runner must kill.
  * A session is a file in ``state_dir``: the steps that completed. ``--resume <id>`` loads it, so a
    resumed run can be seen to continue the same work.
  * Every stdin line, tool start and end, replay and exit is logged with a timestamp to ``log``.

The scenario is a JSON object passed to ``main``. Keys: ``steps`` (list of {name, seconds}),
``state_dir``, ``log``, ``version``, ``ignore_interrupt``, ``ignore_sigterm``, ``late_window``
(seconds the final turn waits before it reports, during which new messages stay queued),
``no_echo``, ``result_usage`` ({input_tokens, output_tokens}), ``cost``.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


def main(scenario: Dict[str, Any], argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(scenario.get("version", "2.1.303 (Claude Code)"))
        return 0
    fake = Fake(scenario, argv)
    return fake.run()


class Fake:
    def __init__(self, scenario: Dict[str, Any], argv: List[str]) -> None:
        self.scenario = scenario
        self.argv = argv
        self.lines: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.pending: List[str] = []
        self.session_id = self.arg_after("--resume") or self.arg_after("--session-id") or str(uuid.uuid4())
        self.resumed = self.arg_after("--resume") is not None
        self.ran: List[str] = []
        self.turns = 0
        self.stdin_closed = False
        self.t0 = time.time()
        if scenario.get("ignore_sigterm"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

    # --- plumbing -----------------------------------------------------------------------

    def arg_after(self, flag: str) -> Optional[str]:
        if flag in self.argv:
            i = self.argv.index(flag)
            if i + 1 < len(self.argv):
                return self.argv[i + 1]
        return None

    def log(self, event: str, **fields: Any) -> None:
        path = self.scenario.get("log")
        if not path:
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": round(time.time() - self.t0, 3), "event": event, **fields}) + "\n")

    def emit(self, obj: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def state_path(self) -> str:
        return os.path.join(self.scenario["state_dir"], f"{self.session_id}.json")

    def save(self) -> None:
        with open(self.state_path(), "w", encoding="utf-8") as fh:
            json.dump({"ran": self.ran}, fh)

    def read_stdin(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            self.log("stdin", line=raw[:200])
            try:
                self.lines.put(json.loads(raw))
            except ValueError:
                continue
        self.lines.put(None)

    # --- the model ----------------------------------------------------------------------

    def wait_or_interrupt(self, seconds: float) -> bool:
        """Spend ``seconds`` on a tool. Returns True when an interrupt ended it early."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                item = self.lines.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is None:
                self.stdin_closed = True
                return False
            if self.absorb(item):
                return True
        return False

    def absorb(self, item: Dict[str, Any]) -> bool:
        """Take one stdin item. True when it is an honoured interrupt."""
        if item.get("type") == "control_request" and (item.get("request") or {}).get("subtype") == "interrupt":
            if self.scenario.get("ignore_interrupt"):
                self.log("interrupt_ignored")
                return False
            self.emit({"type": "control_response",
                       "response": {"subtype": "success", "request_id": item.get("request_id"),
                                    "response": {"still_queued": []}}})
            self.log("interrupt")
            return True
        if item.get("type") == "user":
            content = (item.get("message") or {}).get("content")
            text = content if isinstance(content, str) else " ".join(
                str(b.get("text", "")) for b in (content or []) if isinstance(b, dict))
            self.pending.append(text)
        return False

    def replay_pending(self, steps: List[Dict[str, Any]]) -> None:
        """A model turn begins: read what the person said meanwhile, replay it and obey it."""
        while self.pending:
            text = self.pending.pop(0)
            if not self.scenario.get("no_echo"):
                self.emit({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                           "isReplay": True})
            self.log("replay", text=text)
            words = text.split()
            if len(words) >= 2 and words[0] == "skip":
                steps[:] = [s for s in steps if s["name"] != words[1]]
            elif len(words) >= 2 and words[0] == "add":
                steps.append({"name": words[1], "seconds": 0})

    def run_turn(self, steps: List[Dict[str, Any]]) -> str:
        """Run steps until none are left. Returns ``done``, ``interrupted`` or ``eof``."""
        self.turns += 1
        while True:
            self.replay_pending(steps)
            if not steps:
                break
            step = steps.pop(0)
            self.emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{len(self.ran)}", "name": "Bash", "input": {"command": step["name"]}}]}})
            self.log("tool_start", name=step["name"])
            if self.wait_or_interrupt(float(step.get("seconds", 0))):
                self.emit({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "interrupted"}]}})
                self.log("tool_interrupted", name=step["name"])
                return "interrupted"
            self.ran.append(step["name"])
            self.save()
            self.emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{len(self.ran)}", "content": step["name"]}]}})
            self.log("tool_end", name=step["name"])
            if self.stdin_closed:
                return "eof"
        window = float(self.scenario.get("late_window", 0))
        if window:
            if self.wait_or_interrupt(window):
                return "interrupted"
        return "done"

    def report(self, subtype: str) -> None:
        text = ("resumed after " + ",".join(self.state_before) + "; " if self.resumed else "") \
            + "ran " + ",".join(self.ran)
        self.emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        usage = self.scenario.get("result_usage", {"input_tokens": 10, "output_tokens": 5})
        self.emit({"type": "result", "subtype": subtype, "is_error": subtype != "success",
                   "result": text, "session_id": self.session_id, "num_turns": self.turns,
                   "total_cost_usd": float(self.scenario.get("cost", 0.001)), "usage": usage})
        self.log("result", subtype=subtype, ran=list(self.ran))

    def run(self) -> int:
        self.state_before: List[str] = []
        if self.resumed:
            path = self.state_path()
            if not os.path.exists(path):
                sys.stderr.write("No conversation found with session ID: " + self.session_id + "\n")
                return 1
            with open(path, encoding="utf-8") as fh:
                self.state_before = list(json.load(fh).get("ran", []))
            self.ran = list(self.state_before)
        self.log("argv", argv=self.argv, session_id=self.session_id, pid=os.getpid())
        self.emit({"type": "system", "subtype": "init", "session_id": self.session_id})
        threading.Thread(target=self.read_stdin, daemon=True).start()
        steps = [dict(s) for s in self.scenario.get("steps", [])]
        first = self.lines.get()
        if first is None:
            return 0
        self.absorb(first)
        self.pending.clear()   # the first user line is the prompt, not a steering message
        while True:
            how = self.run_turn(steps)
            if how == "eof":
                self.report("success")
                return 0
            self.report("error_during_execution" if how == "interrupted" else "success")
            # A message that arrived during the last window is a new turn; otherwise wait for input.
            while not self.pending:
                try:
                    item = self.lines.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    self.log("exit")
                    return 0
                self.absorb(item)
            steps = []
