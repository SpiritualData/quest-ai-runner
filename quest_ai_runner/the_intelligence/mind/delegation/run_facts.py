"""What a deep run says about itself while it works and when it ends, in the shapes Quest stores and its screens draw.

One ``RunFacts`` lives for one deep run (a chat turn's or a task's). It reads every ``ProgressEvent`` the deep runners emit through the hub's own
adapter (``progress_events``: a raw Claude Code stream line, a ``tokens`` tick with its ceilings, a ``status`` tick with a phase, a session event) and
folds them with the hub's data form for a separate frontend (the ``frontend-interface`` solution, arm ``json_wire``) and its structure view (the
``structure-in-view`` solution, arm ``event_tree``), both reached through ``quest_ai_runner.run``. What it hands back to the run's sink are ordinary
``ProgressEvent``s, so a task's sink posts each as a progress tick of that kind and a chat turn's sink streams it as an event of that type:

  * ``agent``          a helper starting or ending, with its parent and the solution it serves (``agent-started`` / ``agent-ended``)
  * ``structure``      the structure view of the work, sent only when the tree changed and at most every ``STRUCTURE_GAP`` seconds
  * ``spend``          spend and time so far with the ceilings, sent only when it changed and at most every ``SPEND_GAP`` seconds
  * ``tokens``         the cost and calls so far, when the worker reports a cost
  * ``stop_decision``  the hub's own choice to go on (``hub_resume``) or to stop (``resume_stopped``, a ceiling), with its reason code

A fact the run does not know is left out, never sent as zero: no cost reported means no ``cost_usd``, no ceiling means no ceiling key, a structure that
shows nothing is not sent. ``end_facts`` gives the same account once the run is over (``end_reason``, ``resume_handle``, ``spend_usd``, ``elapsed_seconds``,
the ceilings, ``outside_folder``, ``left``, ``stop_decision``) for the terminal report of a task and the ``result`` event of a chat turn. The backend validates
and bounds every one of these again (``run_state_facts``); the bounds here only keep a tick small. Standard library, the hub and ``quest_ai_runner.run``."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from esoi import implementation
from hubofhubs.api import AgentEnded, AgentStarted, Notice, ProgressEvent, RunStarted, StreamJson, progress_events

SPEND_GAP = 4.0          # seconds between two spend ticks
STRUCTURE_GAP = 2.0      # seconds between two structure ticks
MAX_AGENT_TICKS = 200    # helpers announced per run
MAX_EVENTS = 4000        # session events kept per run; the oldest steps go first
MAX_NODES, MAX_DEPTH, MAX_UNPLACED = 60, 6, 20     # the bounds the backend applies again
MAX_OUTSIDE, LEFT_CHARS = 10, 300
CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}(?::[A-Za-z0-9_.\-]{1,48})?$")
HARD_LIMIT_REASONS = {"spend": "ceiling_spend", "time": "ceiling_time"}


def amount(value: Any) -> Optional[float]:
    """A finite number not below zero, or None: what is not reported is never turned into 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def stop_decision_of(end_reason: str, left: str = "") -> Optional[Dict[str, Any]]:
    """The hub's own decision to stop, as Quest stores it, read from how the run ended; None when the end reason names no decision (a finished run, a
    cancel, a soft-limit wrap-up that does not say which ceiling)."""
    head, _, tail = str(end_reason or "").partition(":")
    reason = tail if head == "resume_stopped" else HARD_LIMIT_REASONS.get(tail) if head == "stopped_at_hard_limit" else ""
    if not reason or not CODE.match(reason):
        return None
    out: Dict[str, Any] = {"continue": False, "reason": reason}
    if left:
        out["left"] = left[:LEFT_CHARS]
    return out


def end_facts(results: Iterable[Any]) -> Dict[str, Any]:
    """How a run ended, from its deep results, for the terminal report of a task and the result event of a chat turn. Only what is known is present:
    ``end_reason``, ``resume_handle`` (only for a run that did not finish and is not parked on a person's answer), ``spend_usd``, ``elapsed_seconds``,
    ``ceiling_usd`` and ``ceiling_seconds`` (set by ``RunFacts.finish``), ``outside_folder``, ``left`` and ``stop_decision``."""
    done = [r for r in results or [] if r is not None]
    if not done:
        return {}
    out: Dict[str, Any] = {}
    reason = next((str(getattr(r, "end_reason", "") or "") for r in reversed(done) if getattr(r, "end_reason", "")), "")
    if reason and CODE.match(reason):
        out["end_reason"] = reason
    unfinished = [r for r in done if not getattr(r, "met", False)]
    if unfinished and not any(getattr(r, "decision_id", None) for r in done):
        handle = next((str(getattr(r, "resume_handle", None) or getattr(r, "session_id", None) or "") for r in reversed(done)
                       if (getattr(r, "resume_handle", None) or getattr(r, "session_id", None))), "")
        if handle:
            out["resume_handle"] = handle
    spent = [v for v in (amount(getattr(r, "spend_usd", None)) for r in done) if v is not None]
    if not spent:
        spent = [v for v in (amount(getattr(r, "cost_usd", None)) for r in done) if v]       # a reported 0.0 default says nothing
    if spent:
        out["spend_usd"] = round(sum(spent), 6)
    elapsed = [v for v in (amount(getattr(r, "elapsed_seconds", None)) for r in done) if v is not None]
    if elapsed:
        out["elapsed_seconds"] = round(max(elapsed), 3)
    for key in ("ceiling_usd", "ceiling_seconds"):
        value = next((v for v in (amount(getattr(r, key, None)) for r in done) if v is not None), None)
        if value is not None:
            out[key] = value
    outside: List[str] = []
    for r in done:
        for item in getattr(r, "outside_folder", None) or []:
            line = " ".join(str(item).split())[:200]
            if line and line not in outside and len(outside) < MAX_OUTSIDE:
                outside.append(line)
    if outside:
        out["outside_folder"] = outside
    left = ""
    for r in unfinished:
        left = " ".join(str(getattr(r, "verdict_next_action", "") or getattr(r, "verdict_reason", "") or "").split())[:LEFT_CHARS]
        if left:
            break
    if left:
        out["left"] = left
    decision = stop_decision_of(out.get("end_reason", ""), left)
    if decision:
        out["stop_decision"] = decision
    return out


def bounded_structure(view: Dict[str, Any]) -> Dict[str, Any]:
    """The structure view cut to the bounds Quest keeps (nodes in all, depth, unplaced helpers), with the key the screens ignore left out."""
    budget = [MAX_NODES]

    def cut(node: Dict[str, Any], depth: int) -> Optional[Dict[str, Any]]:
        if depth > MAX_DEPTH or budget[0] <= 0:
            return None
        budget[0] -= 1
        kept = {k: v for k, v in node.items() if k != "children"}
        kept["children"] = [c for c in (cut(child, depth + 1) for child in node.get("children") or []) if c is not None]
        return kept

    tree = [n for n in (cut(node, 1) for node in view.get("tree") or []) if n is not None]
    return {"tree": tree, "unplaced": list(view.get("unplaced") or [])[:MAX_UNPLACED], "unknown": list(view.get("unknown") or [])[:MAX_NODES]}


def names_of(view: Dict[str, Any]) -> Dict[str, str]:
    """{solution id: its name} over a structure view."""
    names: Dict[str, str] = {}

    def walk(nodes: Iterable[Dict[str, Any]]) -> None:
        for node in nodes:
            names[str(node.get("solution"))] = str(node.get("name") or node.get("solution"))
            walk(node.get("children") or [])

    walk(view.get("tree") or [])
    return names


@implementation("quest-work-shown", "run_facts")
class RunFacts:
    """The run's own account of itself, built from its progress events and handed to ``send`` as progress events of the kinds Quest takes."""
    REQUIRES = ()     # reads the events it is given; the hub's two views are reached through ``quest_ai_runner.run``

    def __init__(self, send: Callable[[ProgressEvent], None], *, goal: str = "", records: Any = None,
                 clock: Callable[[], float] = time.monotonic, spend_gap: float = SPEND_GAP, structure_gap: float = STRUCTURE_GAP):
        self.send = send
        self.records = records
        self.clock = clock
        self.spend_gap, self.structure_gap = spend_gap, structure_gap
        self.started = clock()
        self.stream = StreamJson()
        self.events: List[Any] = [RunStarted(goal=" ".join(goal.split())[:200])] if goal else []
        self.ceilings: Dict[str, float] = {}
        self.names: Dict[str, str] = {}
        self.agent_ticks = 0
        self.last_cost: Optional[float] = None
        self.last_spend: Optional[str] = None
        self.last_structure: Optional[str] = None
        self.spend_at = self.structure_at = float("-inf")
        self.dirty_structure = False
        self.dirty_spend = False
        self.cost_sent_raw = False
        self.lock = threading.RLock()

    # --- reading the run -----------------------------------------------------------------------------------------------------------
    def take(self, event: Any) -> None:
        """Read one progress event of the run. Never raises: a fact that cannot be read is left out, the run goes on."""
        try:
            items = progress_events(event, stream=self.stream)
        except Exception:  # noqa: BLE001
            return
        if not items:
            return
        with self.lock:
            # A ``tokens`` tick that already carries the cost reaches the sink as it is, so its cost is not sent a second time.
            self.cost_sent_raw = getattr(event, "type", "") == "tokens" and amount((getattr(event, "data", None) or {}).get("cost_usd")) is not None
            for item in items:
                self.apply(item)
            self.flush(force=False)

    def apply(self, item: Any) -> None:
        self.events.append(item)
        if len(self.events) > MAX_EVENTS:
            drop = next((i for i, e in enumerate(self.events) if getattr(e, "type", "") == "step"), None)
            del self.events[drop if drop is not None else 0]
        kind = getattr(item, "type", "")
        if kind == "agent-started" and isinstance(item, AgentStarted):
            self.announce_started(item)
            self.dirty_structure = True
        elif kind == "agent-ended" and isinstance(item, AgentEnded):
            self.announce_ended(item)
            self.dirty_structure = True
        elif kind == "usage":
            for key, value in (("ceiling_usd", getattr(item, "ceiling_usd", None)), ("ceiling_seconds", getattr(item, "ceiling_seconds", None))):
                known = amount(value)
                if known is not None and known > 0:
                    self.ceilings[key] = known
            self.dirty_spend = True
        elif kind == "notice" and isinstance(item, Notice) and item.kind == "decision" and item.decision == "continue":
            data: Dict[str, Any] = {"continue": True, "decision": "continue"}
            if CODE.match(item.reason or ""):
                data["reason"] = item.reason
            self.emit("stop_decision", item.text, data)

    def announce_started(self, item: AgentStarted) -> None:
        if self.agent_ticks >= MAX_AGENT_TICKS:
            return
        self.agent_ticks += 1
        agent_tick: Dict[str, Any] = {"type": "agent-started", "agent_id": item.agent_id, "parent_id": item.parent_id or "main", "task": item.task}
        if item.solution:
            agent_tick["solution_id"] = item.solution
            if item.solution in self.names:
                agent_tick["solution_name"] = self.names[item.solution]
        self.emit("agent", "", agent_tick)

    def announce_ended(self, item: AgentEnded) -> None:
        if self.agent_ticks >= MAX_AGENT_TICKS:
            return
        self.agent_ticks += 1
        agent_tick: Dict[str, Any] = {"type": "agent-ended", "agent_id": item.agent_id, "state": item.state}
        if item.calls:
            agent_tick["calls"] = item.calls
        self.emit("agent", "", agent_tick)

    # --- what goes out -------------------------------------------------------------------------------------------------------------
    def emit(self, kind: str, text: Any, data: Dict[str, Any]) -> None:
        try:
            self.send(ProgressEvent(type=kind, text=str(text) if text else None, data=data))
        except Exception:  # noqa: BLE001 -- a sink that fails never stops the run
            pass

    def picture(self, structure: Any = None) -> Dict[str, Any]:
        """The session as the hub's data form for a separate frontend: cost, helpers, changed files, structure."""
        from quest_ai_runner import run
        return run("frontend-interface", {"events": list(self.events), "controls": [], "structure": structure})

    def view(self) -> Dict[str, Any]:
        """The structure of the work: the selected arm of ``structure-in-view`` (``event_tree``, the hub's ``structure_view``) over the run's events."""
        from quest_ai_runner import run
        return run("structure-in-view", {"events": list(self.events), "records": self.records, "expanded": []})

    def flush(self, force: bool) -> None:
        """Send the structure and the spend when they changed and their gap has passed (``force``: now, for the end of the run)."""
        now = self.clock()
        try:
            if self.dirty_structure and (force or now - self.structure_at >= self.structure_gap):
                self.dirty_structure, self.structure_at = False, now
                view = bounded_structure(self.view())
                self.names = names_of(view)
                if view["tree"] or view["unplaced"]:
                    text = json.dumps(view, sort_keys=True, default=str)
                    if text != self.last_structure:
                        self.last_structure = text
                        self.emit("structure", "", view)
            if self.dirty_spend and (force or now - self.spend_at >= self.spend_gap):
                self.dirty_spend = False
                cost = self.picture()["cost"]
                if amount(cost.get("usd")) is not None:         # the gap counts from a spend that was sent, not from one that said nothing
                    self.spend_at = now
                self.send_spend(cost, now)
        except Exception:  # noqa: BLE001 -- the account of a run never stops the run
            pass

    def send_spend(self, cost: Dict[str, Any], now: float, spend: Optional[float] = None, elapsed: Optional[float] = None) -> None:
        usd = amount(cost.get("usd")) if spend is None else spend
        if usd is None:
            return
        if usd != self.last_cost:
            self.last_cost = usd
            tick: Dict[str, Any] = {"cost_usd": usd}
            calls = cost.get("calls")
            if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
                tick["calls"] = calls
            if not self.cost_sent_raw:
                self.emit("tokens", "", tick)
        data: Dict[str, Any] = {"spend_usd": usd, "elapsed_seconds": round(elapsed if elapsed is not None else now - self.started, 1), **self.ceilings}
        text = json.dumps({k: v for k, v in data.items() if k != "elapsed_seconds"}, sort_keys=True)
        if text != self.last_spend:
            self.last_spend = text
            self.emit("spend", "", data)

    # --- the end ---------------------------------------------------------------------------------------------------------------------
    def finish(self, results: Iterable[Any]) -> Dict[str, Any]:
        """The run is over: send what is still waiting, the final spend and the stop decision, and put the ceilings on the results so ``end_facts`` has them.
        Returns ``end_facts(results)``."""
        done = [r for r in results or [] if r is not None]
        with self.lock:
            for key, value in self.ceilings.items():
                for result in done:
                    if amount(getattr(result, key, None)) is None:
                        try:
                            setattr(result, key, value)
                        except Exception:  # noqa: BLE001
                            pass
            self.flush(force=True)
            facts = end_facts(done)
            try:
                if "spend_usd" in facts:
                    self.send_spend({}, self.clock(), spend=facts["spend_usd"], elapsed=facts.get("elapsed_seconds"))
                if "stop_decision" in facts:
                    self.emit("stop_decision", "", dict(facts["stop_decision"]))
            except Exception:  # noqa: BLE001
                pass
        return facts


def end_report_data(result: Any) -> Dict[str, Any]:
    """The end facts of a finished turn (an ``OrchestratorResult``), for its terminal report; ``{}`` when it has none.

    A turn with no deep run that ran out of attempts (``turn_outcome``: stopped, ``attempt_cap``) has its own end facts: ``end_reason`` ``attempt_cap`` and ``left``,
    the answer verdict's account of what the goal still needs, so the screen says what is left and not only that the tries ran out."""
    facts = end_facts(getattr(result, "deep_results", None) or [])
    if facts or getattr(result, "deep_results", None):
        return facts
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import turn_outcome
    outcome = turn_outcome(result)
    if outcome.get("outcome") != "stopped" or outcome.get("stop_reason") != "attempt_cap":
        return {}
    verdict = getattr(result, "goal_verdict", None)
    verdict = verdict if isinstance(verdict, dict) else {}
    out: Dict[str, Any] = {"end_reason": "attempt_cap"}
    left = " ".join(str(verdict.get("next_action") or verdict.get("reason") or "").split())[:LEFT_CHARS]
    if left:
        out["left"] = left
    return out
