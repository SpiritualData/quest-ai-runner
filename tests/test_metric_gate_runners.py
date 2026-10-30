"""The metric record around every kind of deep runner that changes files, each run through its real class.

Joshua's rule is that every run records "the relevant metric result before and after your change" and that code, not the
worker's words, enforces it. The flow can only enforce it where it can tell that a run CHANGED something, so these tests drive
each runner type through the real goal loop and pin what happens when it does:

  * ``SubprocessGoalRunner`` and ``StreamJsonGoalRunner`` (Claude Code): the change is read from the worker's own session
    record (``changes_landed``), the worker is asked once, in its own session, to record a result, and the answer is kept;
  * ``AcpDeepRunner``: the change is read from its tool-call updates; it cannot resume a session, so it is not asked and the
    gap is marked;
  * ``FastEditRunner`` and ``MCPOperationRunner`` (the first rung of a ladder): the change is what the writer accepted; there
    is no worker to ask, so the gap is marked unless the code measured the metric itself.

Only the process edge (Popen, the ACP connection, the model provider) is faked; the runner classes, the orchestrator's loop,
the tool registry and the metric ledger are the real ones. ``run_scenario`` is also what the before/after measurement of this
mechanism calls, so it asks nothing of the runners beyond their public behaviour. Offline: no model, no ``claude`` binary.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from esoi import scaffold

from hubofhubs.act.fast_edit import FastEditRunner
from hubofhubs.act.mcp_operations import MCPOperationRunner
from hubofhubs.ports.deep import WriteResult
from quest_ai_runner.adapters import acp_deep_runner as acp
from quest_ai_runner.adapters.files_writer import FilesWriter
from quest_ai_runner.core import goal_runner_stream
from quest_ai_runner.core.goal_metrics import NUDGE_BRIEF
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.goal_runner_stream import StreamJsonGoalRunner
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import PlanDecision
from quest_ai_runner.core.tools import ToolContext, build_tool_registry

from .conftest import StubProvider
from .test_acp_deep_runner import FakeConn, config as acp_config, install_fake_connection
from .test_fast_edit_ladder import _orch

SHORT_DOC = """# Project notes

Status: paused pending review.

Owner: the platform team.
"""


class ScriptedProvider:
    """A ModelProvider whose ``answer`` replays scripted replies."""

    def __init__(self, replies: List[str]):
        self.replies = list(replies)

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        raise AssertionError("the fast edit path must not call plan()")

    def answer(self, messages, *, model, system=None, layers=None) -> str:
        return self.replies.pop(0) if self.replies else ""

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6", "claude-opus-4-8"]


class ScriptedPlanProvider:
    """A ModelProvider whose ``plan`` replays scripted decisions."""

    def __init__(self, decisions: List[Dict[str, Any]]):
        self.decisions = list(decisions)

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        return self.decisions.pop(0)

    def answer(self, messages, *, model, system=None, layers=None) -> str:
        raise AssertionError("the mcp operation runner must not call answer()")

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6", "claude-opus-4-8"]


class FakeWriter:
    """An OperationWriter double: a scripted catalog and a scripted write result."""

    def __init__(self, catalog: Optional[List[Dict[str, Any]]] = None, result: Optional[WriteResult] = None):
        self.catalog = catalog or []
        self.result = result or WriteResult(ok=True, rel_path="srv:op")

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        return list(self.catalog)

    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult:
        return self.result


CATALOG = [{
    "name": "srv:comment", "description": "Post a comment",
    "input_schema": {"type": "object", "properties": {"body": {"type": "string"}}},
}]

REQUEST = "make the greeting function greet a person by name in the standard form"
GOAL = REQUEST + " in docs/notes.md"
TASK = "task_1"
NA_REASON = "Edited a draft; no metric measures it."

# What a scripted worker does about the metric: nothing, ever; only when asked; or on its own in the first attempt.
IGNORES, ANSWERS_THE_REQUEST, RECORDS_FIRST = "ignores", "answers_the_request", "records_first"
KINDS = ("subprocess", "stream_json", "acp", "fast_edit", "mcp_write")


class StubMeter:
    """A meter that answers 0.5 before and 0.6 after, and takes (no) time."""
    name, cheap = "stub", True

    def __init__(self) -> None:
        self.values = [0.5, 0.6]

    def measure(self, spec_slice, working_dir=None):
        return self.values.pop(0) if self.values else None


class ScriptedWorld:
    """What the worker's shell reaches: the real tool registry, and a Claude Code session record on disk."""

    def __init__(self, root: Path, behavior: str) -> None:
        self.root, self.behavior = root, behavior
        self.registry = build_tool_registry({"QAR_METRIC_LEDGER_DIR": str(root / "metrics"),
                                             "QAR_TOOL_LEDGER_DIR": str(root / "tools")})
        self.run_id: Optional[str] = None
        self.runs: List[Dict[str, Any]] = []   # one entry per time the worker was started: resumed or not
        self.uses = 0

    def session_path(self, session_id: str) -> Path:
        return self.root / "claude" / "projects" / "proj" / f"{session_id}.jsonl"

    def record_edit(self, session_id: str) -> None:
        """The worker edits a file: a tool_use and its (successful) result, appended to its session record."""
        self.uses += 1
        use_id = f"toolu_{self.uses}"
        lines = [{"type": "assistant", "message": {"role": "assistant", "content": [
                      {"type": "tool_use", "id": use_id, "name": "Edit", "input": {"file_path": "/work/docs/notes.md"}}]}},
                 {"type": "user", "message": {"role": "user", "content": [
                      {"tool_use_id": use_id, "type": "tool_result", "content": "ok"}]}}]
        path = self.session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(line) + "\n" for line in lines)

    def declare_not_applicable(self) -> None:
        out = self.registry.invoke("record_metric_result", {"not_applicable": True, "reason": NA_REASON},
                                   ToolContext(meta={"run_id": self.run_id}))
        assert out.ok, out.text

    def work(self, session_id: str, resumed: bool, prompt: str, edits: bool = True) -> None:
        """One run of the worker, started or resumed. The run id is read from the brief, as a real worker would."""
        found = re.search(r"--run (\w+)", prompt)
        self.run_id = self.run_id or (found.group(1) if found else None)
        self.runs.append({"resumed": resumed, "prompt": prompt, "session": session_id})
        if not resumed:
            if edits:
                self.record_edit(session_id)
            if self.behavior == RECORDS_FIRST:
                self.declare_not_applicable()
        elif self.behavior == ANSWERS_THE_REQUEST:
            self.declare_not_applicable()

    def asked(self) -> bool:
        return any(NUDGE_BRIEF in run["prompt"] for run in self.runs)


ENVELOPE = {"type": "result", "subtype": "success", "is_error": False, "result": "Edited the notes.",
            "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}


# -- the process edges --------------------------------------------------------------------------------

class OneShotProcess:
    """``claude -p``: the whole run happens inside ``communicate``, which returns the JSON envelope."""
    returncode, pid = 0, 0
    stdin = None

    def __init__(self, world: ScriptedWorld, cmd: List[str]) -> None:
        self.world = world
        self.resumed = "--resume" in cmd
        self.session_id = cmd[cmd.index("--resume" if self.resumed else "--session-id") + 1]

    def communicate(self, input=None, timeout=None):  # noqa: A002
        self.world.work(self.session_id, self.resumed, (input or b"").decode("utf-8"))
        return json.dumps(ENVELOPE).encode(), b""

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class LineFeed:
    """The stdout of a stream-json session: lines pushed by the fake, read by the runner's reader thread."""

    def __init__(self) -> None:
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def put(self, event: Dict[str, Any]) -> None:
        self.lines.put(json.dumps(event) + "\n")

    def close(self) -> None:
        self.lines.put(None)

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class StreamProcess:
    """``claude -p --input-format stream-json``: the prompt written to stdin starts the run; events come back on stdout."""
    pid = 0

    def __init__(self, world: ScriptedWorld, cmd: List[str]) -> None:
        self.world = world
        self.resumed = "--resume" in cmd
        self.session_id = cmd[cmd.index("--resume" if self.resumed else "--session-id") + 1]
        self.stdout, self.stderr, self.returncode = LineFeed(), iter(()), None
        self.stdin = SimpleNamespace(write=self.write, flush=lambda: None, close=self.stdout.close)

    def write(self, line: str) -> None:
        prompt = json.loads(line)["message"]["content"]
        self.stdout.put({"type": "system", "subtype": "init", "session_id": self.session_id})
        self.world.work(self.session_id, self.resumed, prompt)
        self.stdout.put({**ENVELOPE, "session_id": self.session_id, "num_turns": 1})

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        return None


class AcpConn(FakeConn):
    """The ACP agent: an edit tool call that completes (kind ``edit``, as the protocol types it), then the turn ends."""

    def __init__(self, world: ScriptedWorld) -> None:
        super().__init__(updates=[
            {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "Edit notes", "kind": "edit",
             "status": "pending", "_meta": {"claudeCode": {"toolName": "Edit"}}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "completed"},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Edited the notes."},
             "messageId": "m1"}])
        self.world = world

    async def prompt(self, *, session_id, prompt):
        self.world.work(session_id, False, "".join(str(p.get("text", "")) for p in prompt), edits=False)
        return await super().prompt(session_id=session_id, prompt=prompt)


# -- building one scenario ------------------------------------------------------------------------------

def terminal_runner(world: ScriptedWorld, work_dir: Path, monkeypatch, *, stream: bool = False):
    """A real Claude Code runner whose process is the scripted one. It is the last rung of every ladder here."""
    cfg = SubprocessConfig(working_dir=str(work_dir), claude_path="/usr/bin/claude", review_interval_seconds=0,
                           cancel_grace_seconds=0.5)
    if stream:
        monkeypatch.setattr(goal_runner_stream, "cli_version", lambda binary, env: (2, 1, 300))
        runner = StreamJsonGoalRunner(cfg)
        runner.echo_wait_seconds = 0.2
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: StreamProcess(world, cmd))
        return runner
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: OneShotProcess(world, cmd))
    return SubprocessGoalRunner(cfg)


def build_ladder(kind: str, world: ScriptedWorld, work_dir: Path, monkeypatch) -> List[Any]:
    if kind in ("subprocess", "stream_json"):
        return [terminal_runner(world, work_dir, monkeypatch, stream=kind == "stream_json")]
    if kind == "acp":
        install_fake_connection(monkeypatch, AcpConn(world))
        return [acp.AcpDeepRunner(acp_config(working_dir=str(work_dir)))]
    # The first rung is one model call applied in process; a Claude Code worker is the rung behind it.
    behind = terminal_runner(world, work_dir, monkeypatch)
    if kind == "fast_edit":
        corpus = world.root / "corpus"
        (corpus / "docs").mkdir(parents=True, exist_ok=True)
        (corpus / "docs" / "notes.md").write_text(SHORT_DOC)
        reply = "docs/notes.md\n```\n" + SHORT_DOC.replace("paused pending review", "active") + "```\n"
        edit_provider = ScriptedProvider([reply])
        first = FastEditRunner(provider=edit_provider, registry=ModelRegistry(edit_provider),
                               writer=FilesWriter(str(corpus)))
    else:
        plan_provider = ScriptedPlanProvider([{"operation": "srv:comment", "args": {"body": "hi"}}])
        first = MCPOperationRunner(
            provider=plan_provider,
            registry=ModelRegistry(plan_provider),
            writer=FakeWriter(catalog=CATALOG, result=WriteResult(ok=True, rel_path="srv:comment")))
    return [first, behind]


def run_scenario(kind: str, behavior: str, with_slice: bool, root: Path, monkeypatch) -> Dict[str, Any]:
    """One file-changing deep goal through the real goal loop. Returns what the metric record ended as."""
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "claude"))
    world = ScriptedWorld(root, behavior)
    work_dir = root / "work"
    if with_slice:
        scaffold.init(work_dir, "demo_pkg", say=lambda *_: None)
    else:
        work_dir.mkdir()
    ladder = build_ladder(kind, world, work_dir, monkeypatch)
    orch = _orch(StubProvider([]), ladder, [{"met": True}] * 4)
    orch.tools = world.registry
    if hasattr(orch.goal_iteration, "meters"):   # the arm that measures: read with a stub, not a real arm
        orch.goal_iteration.meters = [StubMeter()]
    plan = PlanDecision(action="deep", goal=GOAL, deep_brief=GOAL)
    done = orch._run_deep(plan, REQUEST, "sonnet", ctx_meta={"task_id": TASK}).deep_results[0]
    sources = [m["source"] for m in done.metric_results]
    return {"sources": sources, "ended_with_a_row": bool(sources), "marked_missing": bool(done.metric_missing),
            "asked": world.asked(), "worker_starts": len(world.runs), "result": done, "runs": world.runs,
            "changes_landed": getattr(done, "changes_landed", None)}


# -- the Claude Code runners ----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["subprocess", "stream_json"])
def test_a_claude_code_worker_that_edited_is_asked_once_in_its_own_session_and_its_answer_is_kept(kind, tmp_path, monkeypatch):
    out = run_scenario(kind, ANSWERS_THE_REQUEST, False, tmp_path, monkeypatch)
    assert out["asked"] and out["worker_starts"] == 2   # started, then resumed with the one request
    first, second = out["runs"]
    assert not first["resumed"] and second["resumed"] and second["session"] == first["session"]   # the same session
    assert NUDGE_BRIEF in second["prompt"] and NUDGE_BRIEF not in first["prompt"]
    assert out["sources"] == ["not_applicable"] and not out["marked_missing"]
    assert out["changes_landed"] == 1   # counted from its session record: the resumed run added none


@pytest.mark.parametrize("kind", ["subprocess", "stream_json"])
def test_a_claude_code_worker_that_ignores_the_request_is_marked_and_not_asked_again(kind, tmp_path, monkeypatch):
    out = run_scenario(kind, IGNORES, False, tmp_path, monkeypatch)
    assert out["worker_starts"] == 2 and out["asked"]   # one request; never a loop
    assert out["sources"] == [] and out["marked_missing"]


@pytest.mark.parametrize("kind", ["subprocess", "stream_json"])
def test_a_claude_code_worker_that_records_on_its_own_is_not_asked(kind, tmp_path, monkeypatch):
    out = run_scenario(kind, RECORDS_FIRST, False, tmp_path, monkeypatch)
    assert not out["asked"] and out["worker_starts"] == 1 and out["sources"] == ["not_applicable"]


@pytest.mark.parametrize("kind", ["subprocess", "stream_json"])
def test_the_worker_is_given_its_run_id_in_its_environment_as_well_as_its_brief(kind, tmp_path, monkeypatch):
    seen: Dict[str, str] = {}
    real = {"subprocess": lambda cmd, **kw: OneShotProcess(world, cmd),
            "stream_json": lambda cmd, **kw: StreamProcess(world, cmd)}[kind]

    def spy(cmd, **kw):
        seen.update(kw.get("env") or {})
        return real(cmd, **kw)

    world = ScriptedWorld(tmp_path, IGNORES)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    runner = terminal_runner(world, tmp_path, monkeypatch, stream=kind == "stream_json")
    monkeypatch.setattr(subprocess, "Popen", spy)
    runner.run_goal(goal="g", brief="b", run_id="abcd1234")
    assert seen.get("QAR_TOOL_RUN_ID") == "abcd1234"


def test_a_session_resumed_to_answer_the_request_counts_only_what_it_added(tmp_path, monkeypatch):
    world = ScriptedWorld(tmp_path, IGNORES)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    runner = terminal_runner(world, tmp_path, monkeypatch)
    first = runner.run_goal(goal="g", brief="b")
    assert first.changes_landed == 1 and first.session_id
    started = world.work

    def edits_again_when_resumed(session_id, resumed, prompt, edits=True):
        started(session_id, resumed, prompt, edits)
        if resumed:
            world.record_edit(session_id)

    world.work = edits_again_when_resumed
    second = runner.run_goal(goal="g", brief="again", resume_session_id=first.session_id)
    assert second.session_id == first.session_id and second.changes_landed == 1   # the first run's edit is not counted again


def test_a_worker_that_changed_nothing_is_never_asked(tmp_path, monkeypatch):
    world = ScriptedWorld(tmp_path, IGNORES)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    runner = terminal_runner(world, tmp_path, monkeypatch)
    world.work = lambda session_id, resumed, prompt, edits=True: world.runs.append({"prompt": prompt})   # reads only
    res = runner.run_goal(goal="g", brief="b")
    assert res.changes_landed == 0


# -- the ACP runner -------------------------------------------------------------------------------------

def test_an_acp_agent_that_completed_an_edit_call_is_known_to_have_changed_something(tmp_path, monkeypatch):
    out = run_scenario("acp", IGNORES, False, tmp_path, monkeypatch)
    assert out["changes_landed"] == 1
    assert not out["asked"] and out["marked_missing"]   # it cannot resume a session, so it is marked, not asked


def test_the_acp_agent_is_given_the_run_id_in_its_environment(tmp_path):
    runner = acp.AcpDeepRunner(acp_config(working_dir=str(tmp_path)))
    assert runner.build_env(run_id="abcd1234")["QAR_TOOL_RUN_ID"] == "abcd1234"
    assert "QAR_TOOL_RUN_ID" not in runner.build_env()


def test_an_acp_edit_that_failed_or_a_read_that_completed_is_no_change():
    kinds: Dict[str, str] = {}
    names: Dict[str, str] = {}
    from quest_ai_runner.adapters.acp_deep_runner_protocol import landed_file_change
    assert landed_file_change({"sessionUpdate": "tool_call", "toolCallId": "e1", "kind": "edit", "status": "pending"},
                              kinds, names) is None
    assert landed_file_change({"sessionUpdate": "tool_call_update", "toolCallId": "e1", "status": "failed"},
                              kinds, names) is None
    assert landed_file_change({"sessionUpdate": "tool_call", "toolCallId": "r1", "kind": "read", "status": "completed"},
                              kinds, names) is None
    assert landed_file_change({"sessionUpdate": "tool_call_update", "toolCallId": "e1", "status": "completed"},
                              kinds, names) == "e1"   # the kind arrived on the first update, the status on a later one


def test_an_acp_tool_named_as_a_file_edit_counts_when_it_declares_no_kind():
    kinds: Dict[str, str] = {}
    names: Dict[str, str] = {}
    from quest_ai_runner.adapters.acp_deep_runner_protocol import landed_file_change
    update = {"sessionUpdate": "tool_call", "toolCallId": "w1", "status": "completed",
              "_meta": {"claudeCode": {"toolName": "Write"}}}
    assert landed_file_change(update, kinds, names) == "w1"


# -- the first rung of a ladder -------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["fast_edit", "mcp_write"])
def test_a_rung_that_changed_something_and_has_no_worker_to_ask_is_marked(kind, tmp_path, monkeypatch):
    out = run_scenario(kind, IGNORES, False, tmp_path, monkeypatch)
    assert out["changes_landed"] == 1 and out["worker_starts"] == 0   # it landed the change; the worker behind it never ran
    assert not out["asked"] and out["sources"] == [] and out["marked_missing"]


@pytest.mark.parametrize("kind", KINDS)
def test_the_selected_arm_writes_a_measured_row_for_every_runner_type_when_a_slice_resolves(kind, tmp_path, monkeypatch):
    out = run_scenario(kind, IGNORES, True, tmp_path, monkeypatch)
    assert out["sources"] == ["measured"] and not out["marked_missing"]
    entry = out["result"].metric_results[0]
    assert (entry["before"], entry["after"], entry["solution_id"]) == (0.5, 0.6, "greet-a-person")


@pytest.mark.parametrize("kind", ["fast_edit", "mcp_write"])
def test_a_change_the_rung_landed_counts_even_when_the_run_is_not_verified_met(kind, tmp_path, monkeypatch):
    """The count is the runner's record of what landed, not of whether the goal was met."""
    out = run_scenario(kind, IGNORES, False, tmp_path, monkeypatch)
    assert out["result"].changes_landed >= 1


def test_the_environment_variable_that_turns_the_measuring_off_leaves_the_gate_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC", "generic-doctrine")
    out = run_scenario("subprocess", ANSWERS_THE_REQUEST, True, tmp_path, monkeypatch)
    assert out["asked"] and out["sources"] == ["not_applicable"]   # no measured row; the worker's own is the record
