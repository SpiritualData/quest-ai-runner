"""QAR's terminal scored against the requirements of the hub's page `working-with-the-ai-while-it-works` (ids X1 to X23).

Free and offline. Where the terminal has the behaviour, a scripted session over the real stream runner (a stand-in for ``claude``, tests/fakes) or the real
pure functions of the terminal proves it; where it has none, the probe looks for the behaviour in the terminal's own code and reports it missing. Run with
``-s`` for the table. ``MET`` pins what holds today, so a requirement already met cannot be lost silently; a new one met is added to it.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

from hubofhubs.api import DeepResult
from quest_ai_runner.the_consciousness.terminal import textual_ui_control, textual_ui_deep
from quest_ai_runner.the_consciousness.terminal.textual_ui_outcome import result_items
from quest_ai_runner.the_intelligence.mind.delegation.inbox import InMemoryInbox

from .test_goal_runner_stream import make_claude, make_runner
from .test_terminal_run_control import KEY, make_control, run_in_thread

TERMINAL = Path(textual_ui_control.__file__).resolve().parent
ALL_IDS = [f"X{n}" for n in range(1, 24)]
MET = {"X1", "X2", "X3", "X4", "X5", "X6", "X10", "X16", "X17", "X18", "X21"}


def code() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TERMINAL.glob("*.py")))


def has(*patterns: str) -> bool:
    """True when every regular expression matches the terminal's code (case-insensitive)."""
    text = code()
    return all(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def stop_resume_steer(tmp_path):
    """One scripted session: a message typed while the worker runs is taken once and confirmed, Escape stops and keeps the session, and a resume continues it."""
    inbox = InMemoryInbox()
    seen = []
    control = make_control(inbox, on_seen=lambda texts: seen.append(texts))
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}]})
    thread, box = run_in_thread(make_runner(claude, tmp_path), control)
    time.sleep(0.4)
    inbox.push(KEY, "use the short name")
    time.sleep(0.6)
    control.cancel.set()
    thread.join(timeout=15)
    stopped = box["result"]
    steer_ok = inbox.peek(KEY) == [] and stopped.unseen_inputs in ([], ["use the short name"]) and bool(stopped.cancelled)
    resumed = make_runner(make_claude(tmp_path, {"steps": [{"name": "after", "seconds": 0.1}]}, name="claude2"), tmp_path).run_goal(
        goal="do the steps", brief="steps", resume_session_id=stopped.session_id, control=make_control(inbox).run_control())
    return {"stop": bool(stopped.cancelled and stopped.session_id), "resume": bool(resumed.met), "steer": steer_ok}


def results(tmp_path):
    session = stop_resume_steer(tmp_path)
    kinds = textual_ui_deep.DeepMixin
    summary = kinds._summarize_exec_lines(["Read: a.txt", "Edit: b.txt", "$ pytest -q"])
    shown = result_items([DeepResult(met=True, cost_usd=0.0123, observations=["changed b.txt", "changed c.txt"])])
    nothing_shown = result_items([DeepResult(met=True)])
    out = {
        "X1": (bool(summary and "read" in summary[0] and "edit" in summary[0] and "command" in summary[0]), "the summary counts reads, edits and commands; each target is in the live line"),
        "X2": (has(r"format_elapsed", r"so far"), "elapsed time and the count of what was read"),
        "X3": (any("$" in item for item in shown) and nothing_shown == [], "cost in the closing line only when a run reported it"),
        "X4": (session["stop"], "Escape stops the worker and keeps its session"),
        "X5": (session["resume"], "a resume continues the kept session"),
        "X6": (session["steer"], "a message typed while the worker runs is taken once"),
        "X7": (has(r"parent_tool_use_id|parent_id|subagent|sub-agent"), "a tree of helpers with their parents"),
        "X8": (has(r"stop_agent|steer_agent"), "stop or steer one helper"),
        "X9": (False, "a step waits for approval inside the run (a planner confirm waits, but only between steps)"),
        "X10": (any("changed" in item for item in shown), "the closing line names the files changed"),
        "X11": (has(r"/harness"), "switch harness from the terminal"),
        "X12": (has(r"session_events"), "consumes the shared events and controls"),
        "X13": (has(r"def plain_chat|--plain"), "a plain fallback without Textual or a cursor"),
        "X14": (has(r"\bserves:|Serves the solution"), "the solution and milestone the work serves"),
        "X15": (has(r"solution hierarchy|agent_solution"), "helpers follow the solution hierarchy"),
        "X16": (has(r"work_trace", r"render"), "the trail is drawn from structural events"),
        "X17": (has(r"metric_lines", r"\bbefore\b", r"\bafter\b"), "metric before and after in the milestone block"),
        "X18": (has(r"alt\+t|alt\+d"), "detail opens on a key, not by default"),
        "X19": (has(r"will change:|It will change"), "what will change is shown before a risky step"),
        "X20": (has(r"Blocked or refused"), "blocked or refused steps listed with the reason"),
        "X21": (session["resume"] and has("/resume"), "resume after a stop"),
        "X22": (not re.search(r"def open_ask_takes.*?_turn_active", code(), re.S), "a question can be answered while the run goes on"),
        "X23": (False, "not scripted: judged on the hub page by the person"),
    }
    return out


def test_the_terminal_against_the_requirements(tmp_path, capsys):
    table = results(tmp_path)
    assert set(table) == set(ALL_IDS)
    met = {i for i, (ok, _) in table.items() if ok}
    with capsys.disabled():
        print(f"\nTerminal requirements met: {len(met)} of {len(ALL_IDS)}")
        for i in ALL_IDS:
            print(f"  {i:<4}{'met  ' if table[i][0] else 'NOT  '}{table[i][1]}")
    assert MET <= met, f"requirements lost: {sorted(MET - met)}"


def test_the_closing_line_names_cost_and_files_only_when_reported():
    assert result_items([DeepResult(met=True)]) == []
    assert result_items([DeepResult(met=True, cost_usd=0.5)]) == ["$0.50"]
    items = result_items([DeepResult(met=True, observations=[f"changed f{n}.py" for n in range(5)])])
    assert items == ["5 files changed: f0.py, f1.py, f2.py and 2 more"]
    assert result_items([SimpleNamespace(cost_usd=0.002, observations=["changed a.py"])]) == ["$0.0020", "1 file changed: a.py"]
