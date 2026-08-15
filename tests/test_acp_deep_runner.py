"""Offline tests for the ACP deep runner (``adapters/acp_deep_runner.py``).

Fully offline by construction: the optional ``agent-client-protocol`` package is never imported,
no Node process is spawned, no Claude auth is touched, and nothing goes near a socket. The adapter
concentrates all of that behind ONE module-level seam — ``open_agent_connection`` — so these tests
replace that symbol with a scripted fake connection and drive the real adapter end to end.

What is pinned here:
  * the ``DeepRunner`` interface contract (structural check + signature parity with the reference
    ``SubprocessGoalRunner``, since the orchestrator forwards kwargs by signature inspection);
  * ``session/update`` translation into QAR's existing EVENT_EXEC vocabulary, including the rule
    that a tool finishing must NOT use a phase the guard reads as the SUBGOAL finishing;
  * permission mapping onto the existing skip_permissions / allowed_tools / EscalationSink model;
  * a mid-run steering injection actually reaching the live session (the capability this adapter
    exists for), and the requeue-instead-of-lose behaviour when the turn has already settled;
  * graceful degradation: missing package, missing binary, too-old Node, handshake failure.
"""
from __future__ import annotations

import asyncio
import inspect
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters import acp_deep_runner as acp
from quest_ai_runner.core.adapters import DeepRunner, EVENT_EXEC, Escalation
from quest_ai_runner.core.goal_runner import SubprocessGoalRunner
from quest_ai_runner.core.guard import classify_exec_phase
from quest_ai_runner.core.inbox import InMemoryInbox


# --- scripted fake ACP connection --------------------------------------------------------------

def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class FakeConn:
    """A stand-in for ``acp.ClientSideConnection`` driven by a script of session updates."""

    def __init__(
        self,
        *,
        updates: Optional[List[Any]] = None,
        stop_reason: str = "end_turn",
        usage: Any = None,
        steering: bool = True,
        steering_outcome: str = "injected",
        wait_for_steering: bool = False,
        session_id: str = "sess-1",
        config_options: Optional[List[Any]] = None,
        modes: Any = None,
        fail_initialize: Optional[Exception] = None,
        permission_asks: Optional[List[Dict[str, Any]]] = None,
    ):
        self.updates = updates or []
        self.stop_reason = stop_reason
        self.usage = usage
        self.steering = steering
        self.steering_outcome = steering_outcome
        self.wait_for_steering = wait_for_steering
        self.session_id = session_id
        self.config_options = config_options or []
        self.modes = modes
        self.fail_initialize = fail_initialize
        self.permission_asks = permission_asks or []

        self.client: Any = None
        self.steered: List[str] = []
        self.set_options: List[tuple] = []
        self.set_modes: List[str] = []
        self.cancelled = False
        self.closed = False
        self.permission_answers: List[Dict[str, Any]] = []

    async def initialize(self, *, protocol_version: int, client_capabilities: Any = None, **kw: Any):
        if self.fail_initialize is not None:
            raise self.fail_initialize
        return ns(field_meta={"steering": {"supported": self.steering}} if self.steering else {})

    async def new_session(self, *, cwd: str, mcp_servers: Any = None, **kw: Any):
        self.cwd = cwd
        return ns(session_id=self.session_id, config_options=self.config_options, modes=self.modes)

    async def set_config_option(self, *, config_id: str, session_id: str, value: str):
        self.set_options.append((config_id, value))

    async def set_session_mode(self, *, session_id: str, mode_id: str):
        self.set_modes.append(mode_id)

    async def prompt(self, *, session_id: str, prompt: List[Dict[str, Any]]):
        self.prompt_sent = prompt
        for update in self.updates:
            await self.client.session_update(session_id, update)
        for ask in self.permission_asks:
            self.permission_answers.append(
                await self.client.request_permission(session_id, ask["tool_call"], ask["options"])
            )
        if self.wait_for_steering:
            for _ in range(400):            # ~4s ceiling; the steering poll runs every 10ms
                if self.steered:
                    break
                await asyncio.sleep(0.01)
        return ns(stop_reason=self.stop_reason, usage=self.usage)

    async def ext_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert method == acp.STEERING_EXT_METHOD
        assert params["_meta"]["steering"]["idleBehavior"] == "promptRequired"
        self.steered.append(params["prompt"][0]["text"])
        return {"outcome": self.steering_outcome}

    async def cancel(self, *, session_id: str):
        self.cancelled = True

    async def close_session(self, *, session_id: str):
        self.closed = True


def install_fake_connection(monkeypatch, conn: FakeConn, *, spawn_error: Optional[Exception] = None):
    """Replace the ONE seam that touches the SDK/subprocess with a scripted fake."""

    @asynccontextmanager
    async def fake_open(client, argv, *, env, cwd):
        if spawn_error is not None:
            raise spawn_error
        conn.client = client
        conn.argv = argv
        conn.env = env
        client.on_connect(conn)
        yield conn, None

    monkeypatch.setattr(acp, "open_agent_connection", fake_open)
    return conn


def config(**overrides: Any) -> acp.AcpConfig:
    base: Dict[str, Any] = dict(
        working_dir="/work",
        agent_command="/opt/acp/claude-agent-acp",   # not a .js path -> no Node probe needed
        steering_poll_seconds=0.01,
        heartbeat_seconds=1000.0,                    # keep heartbeats out of assertions
        timeout_seconds=20.0,
    )
    base.update(overrides)
    return acp.AcpConfig(**base)


def message_chunk(text: str, message_id: str = "m1") -> Dict[str, Any]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text},
            "messageId": message_id}


# --- 1. the DeepRunner interface contract ------------------------------------------------------

def test_satisfies_the_deep_runner_protocol():
    runner = acp.AcpDeepRunner(config())
    assert isinstance(runner, DeepRunner)
    assert callable(runner.run_goal)


def test_run_goal_signature_matches_the_reference_runner():
    """The orchestrator forwards emit/run_id/context_preamble/working_dir by SIGNATURE INSPECTION.
    A missing kwarg here would silently drop live progress or the per-task working dir."""
    ours = inspect.signature(acp.AcpDeepRunner.run_goal).parameters
    theirs = inspect.signature(SubprocessGoalRunner.run_goal).parameters
    assert set(ours) == set(theirs)
    for name, param in theirs.items():
        assert ours[name].kind is param.kind, name
        assert ours[name].default == param.default, name


def test_web_capability_derives_from_the_same_tool_gating():
    assert config().web_enabled() is True
    assert config(allowed_tools=["Read", "Bash"]).web_enabled() is False
    assert config(allowed_tools=["WebSearch"]).web_enabled() is True
    assert config(disallowed_tools=["WebSearch", "WebFetch"]).web_enabled() is False


def test_runner_is_reported_as_a_code_and_web_capability():
    """``config.derive_capabilities`` reads ``deep_runner.cfg.web_enabled()`` generically, so this
    runner must advertise itself through exactly the same path the subprocess one does."""
    from quest_ai_runner.config import RunnerConfig, derive_capabilities

    cfg = RunnerConfig(deep_runner=acp.AcpDeepRunner(config()))
    caps = derive_capabilities(cfg)
    assert caps["code"] is True and caps["web"] is True

    cfg_no_web = RunnerConfig(deep_runner=acp.AcpDeepRunner(config(disallowed_tools=["WebSearch",
                                                                                     "WebFetch"])))
    assert derive_capabilities(cfg_no_web)["web"] is False


# --- 2. session/update translation -------------------------------------------------------------

def test_agent_message_chunk_becomes_a_message_tick():
    out = acp.translate_session_update(message_chunk("Reading the config"))
    assert out["phase"] == acp.PHASE_MESSAGE
    assert out["text"] == "Reading the config"
    assert out["data"]["message_id"] == "m1"


def test_thought_chunk_is_distinguished_narration():
    out = acp.translate_session_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm " * 60}}
    )
    assert out["phase"] == acp.PHASE_THINKING
    assert out["text"].startswith("[thinking] ")
    assert out["text"].endswith("...")          # bounded like the subprocess runner's renderer


def test_tool_call_start_renders_like_a_claude_code_session_line():
    shell = acp.translate_session_update({
        "sessionUpdate": "tool_call", "toolCallId": "t1", "title": "pytest -q", "kind": "execute",
        "_meta": {"claudeCode": {"toolName": "Bash"}},
    })
    assert shell["phase"] == acp.PHASE_TOOL_CALL
    assert shell["text"] == "$ pytest -q"
    assert shell["data"]["tool_name"] == "Bash"

    read = acp.translate_session_update({
        "sessionUpdate": "tool_call", "toolCallId": "t2", "title": "Read file", "kind": "read",
        "locations": [{"path": "docs/README.md"}],
        "_meta": {"claudeCode": {"toolName": "Read"}},
    })
    assert read["text"] == "Read: docs/README.md"


@pytest.mark.parametrize("status,phase", [
    ("pending", acp.PHASE_TOOL_PROGRESS),
    ("in_progress", acp.PHASE_TOOL_PROGRESS),
    ("completed", acp.PHASE_TOOL_RESULT),
    ("failed", acp.PHASE_TOOL_ERROR),
])
def test_tool_lifecycle_statuses_map_to_tool_phases(status, phase):
    out = acp.translate_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": status, "title": "x"}
    )
    assert out["phase"] == phase


def test_a_finished_tool_is_not_a_finished_subgoal():
    """core/guard.py classifies EVENT_EXEC phases; "completed"/"failed" would mark the whole deep
    task succeeded/failed off ONE tool call. The tool phases must stay non-terminal, and only the
    run's own final tick may be terminal."""
    assert classify_exec_phase(acp.PHASE_TOOL_RESULT) is None
    assert classify_exec_phase(acp.PHASE_TOOL_ERROR) is None
    assert classify_exec_phase(acp.PHASE_TOOL_PROGRESS) is None
    assert classify_exec_phase(acp.PHASE_MESSAGE) is None
    assert classify_exec_phase(acp.PHASE_DONE) == "success"
    assert classify_exec_phase(acp.PHASE_ERROR) == "failure"


def test_plan_update_summarizes_the_entries():
    out = acp.translate_session_update({"sessionUpdate": "plan", "entries": [
        {"content": "a", "status": "completed", "priority": "high"},
        {"content": "b", "status": "pending", "priority": "low"},
    ]})
    assert out["phase"] == acp.PHASE_PLAN
    assert out["text"] == "Plan: 2 task(s), 1 done"
    assert out["data"]["entries"][0]["content"] == "a"


@pytest.mark.parametrize("kind", [
    "user_message_chunk", "available_commands_update", "config_option_update",
    "session_info_update", "usage_update",
])
def test_startup_and_echo_chatter_is_dropped(kind):
    assert acp.translate_session_update({"sessionUpdate": kind}) is None


def test_the_scripted_stream_reaches_the_progress_sink(monkeypatch):
    conn = FakeConn(updates=[
        message_chunk("Looking at the tests"),
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "consider X"}},
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "ls", "kind": "execute",
         "_meta": {"claudeCode": {"toolName": "Bash"}}},
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"},
        message_chunk("Done: updated two files.", message_id="m2"),
    ], usage=ns(input_tokens=100, output_tokens=40))
    install_fake_connection(monkeypatch, conn)

    events = []
    runner = acp.AcpDeepRunner(config())
    result = runner.run_goal(goal="update the docs", brief="do it", emit=events.append,
                             run_id="run-abc")

    assert result.met is True
    assert result.output == "Done: updated two files."   # the LAST message, like `claude -p`'s result
    assert result.tokens == 140
    assert all(e.type == EVENT_EXEC for e in events)
    assert all(e.data["run_id"] == "run-abc" for e in events)
    phases = [e.data["phase"] for e in events]
    assert phases.count(acp.PHASE_MESSAGE) == 2
    assert acp.PHASE_THINKING in phases
    assert acp.PHASE_TOOL_CALL in phases
    assert acp.PHASE_TOOL_RESULT in phases
    assert phases[-1] == acp.PHASE_DONE


# --- 3. stop-reason -> met mapping -------------------------------------------------------------

def test_end_turn_with_no_output_is_a_silent_no_op_not_a_success(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(updates=[]))
    result = acp.AcpDeepRunner(config()).run_goal(goal="do a thing", brief="b")
    assert result.met is False
    assert "NO agent output" in (result.error or "")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "max_turn_requests", "refusal", "cancelled"])
def test_non_clean_stop_reasons_are_explained_not_met(monkeypatch, stop_reason):
    install_fake_connection(monkeypatch, FakeConn(updates=[message_chunk("partial work")],
                                                 stop_reason=stop_reason))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert result.output == "partial work"
    # The reason travels with the result: a human reading the failed task learns WHY it stopped.
    assert result.error == acp.STOP_REASON_FAILURES[stop_reason]
    assert "not " in result.error.lower()


def test_an_unknown_stop_reason_is_never_reported_as_met(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(updates=[message_chunk("x")],
                                                 stop_reason="something_new"))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "unrecognized stop reason" in (result.error or "")


def test_the_escalation_marker_still_pauses_the_run(monkeypatch):
    """A worker that raises a human decision the prose way (the QAR-ESCALATED marker) is honored
    identically here, so the executor reports needs_you with the decision linked."""
    install_fake_connection(monkeypatch, FakeConn(
        updates=[message_chunk("I need approval.\nQAR-ESCALATED: decision-77")]))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert result.decision_id == "decision-77"


# --- 4. permission mapping ---------------------------------------------------------------------

def test_tool_name_comes_from_the_structured_field_only():
    assert acp.tool_name_of({"title": "rm -rf /", "_meta": {"claudeCode": {"toolName": "Bash"}}}) == "Bash"
    # No structured name: we report None rather than guessing from the agent-composed title.
    assert acp.tool_name_of({"title": "Bash"}) is None


def test_permission_policy_reads_the_existing_config_surface():
    assert acp.classify_permission(config(), "Bash")[0] == acp.ALLOW
    assert acp.classify_permission(config(disallowed_tools=["Bash"]), "Bash")[0] == acp.REJECT
    # An explicit denial beats auto-approval even with skip_permissions on.
    assert acp.classify_permission(config(skip_permissions=True, disallowed_tools=["Bash"]),
                                   "Bash")[0] == acp.REJECT
    assert acp.classify_permission(config(allowed_tools=["Read"]), "Bash")[0] == acp.REJECT
    assert acp.classify_permission(config(allowed_tools=["Read"]), "Read")[0] == acp.ALLOW
    # Fails CLOSED: a pinned allow-list plus an unidentifiable tool cannot be approved.
    assert acp.classify_permission(config(allowed_tools=["Read"]), None)[0] == acp.REJECT
    assert acp.classify_permission(config(skip_permissions=False), "Read")[0] == acp.HUMAN


def test_options_are_selected_by_kind_never_by_display_name():
    options = [
        {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
        {"optionId": "allow", "name": "Allow Once", "kind": "allow_once"},
        {"optionId": "allow_always", "name": "Always Allow", "kind": "allow_always"},
    ]
    assert acp.select_option(options, ("allow_always", "allow_once")) == "allow_always"
    assert acp.select_option(options, ("allow_once", "allow_always")) == "allow"
    assert acp.select_option(options, ("reject_once",)) == "reject"
    assert acp.select_option(options, ("nonexistent",)) is None
    assert acp.permission_response(None) == {"outcome": {"outcome": "cancelled"}}


PERMISSION_OPTIONS = [
    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
    {"optionId": "allow", "name": "Allow Once", "kind": "allow_once"},
    {"optionId": "allow_always", "name": "Always Allow", "kind": "allow_always"},
]


def permission_ask(tool_name: str = "Bash") -> Dict[str, Any]:
    return {"tool_call": {"toolCallId": "t1", "title": "pytest -q",
                          "_meta": {"claudeCode": {"toolName": tool_name}}},
            "options": PERMISSION_OPTIONS}


def test_an_autonomous_run_auto_approves_the_ask(monkeypatch):
    conn = FakeConn(updates=[message_chunk("ok")], permission_asks=[permission_ask()])
    install_fake_connection(monkeypatch, conn)
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is True
    assert conn.permission_answers == [{"outcome": {"outcome": "selected",
                                                    "optionId": "allow_always"}}]


def test_a_disallowed_tool_is_denied_even_in_an_autonomous_run(monkeypatch):
    conn = FakeConn(updates=[message_chunk("ok")], permission_asks=[permission_ask("Bash")])
    install_fake_connection(monkeypatch, conn)
    acp.AcpDeepRunner(config(disallowed_tools=["Bash"])).run_goal(goal="g", brief="b")
    assert conn.permission_answers == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]


def test_a_gated_run_escalates_to_a_human_and_returns_needs_you(monkeypatch):
    """skip_permissions off + an EscalationSink = the ask becomes a real decision-request and the
    run comes back paused with the decision linked — the SAME contract the QAR-ESCALATED marker
    gives the subprocess runner, not a parallel permission system."""
    raised: List[Escalation] = []

    class Sink:
        def escalate(self, escalation: Escalation) -> str:
            raised.append(escalation)
            return "decision-42"

    conn = FakeConn(updates=[message_chunk("asking first")], permission_asks=[permission_ask()])
    install_fake_connection(monkeypatch, conn)
    result = acp.AcpDeepRunner(config(skip_permissions=False, escalation=Sink())).run_goal(
        goal="send the mail", brief="b")

    assert len(raised) == 1
    assert raised[0].default_on_silence == "hold"
    assert "Bash" in raised[0].summary
    # The tool itself is denied so the turn cannot proceed past the unapproved step.
    assert conn.permission_answers == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]
    assert result.met is False
    assert result.decision_id == "decision-42"


def test_a_gated_run_with_nobody_to_ask_denies_rather_than_proceeding(monkeypatch):
    conn = FakeConn(updates=[message_chunk("ok")], permission_asks=[permission_ask()])
    install_fake_connection(monkeypatch, conn)
    result = acp.AcpDeepRunner(config(skip_permissions=False)).run_goal(goal="g", brief="b")
    assert conn.permission_answers == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]
    assert result.decision_id is None


def test_a_raising_escalation_sink_degrades_to_a_denial(monkeypatch):
    class BrokenSink:
        def escalate(self, escalation):
            raise RuntimeError("decision service down")

    conn = FakeConn(updates=[message_chunk("ok")], permission_asks=[permission_ask()])
    install_fake_connection(monkeypatch, conn)
    result = acp.AcpDeepRunner(config(skip_permissions=False, escalation=BrokenSink())).run_goal(
        goal="g", brief="b")
    assert conn.permission_answers == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]
    assert result.decision_id is None


# --- 5. mid-run steering (the point of this adapter) -------------------------------------------

def test_a_message_queued_mid_run_reaches_the_turn_already_in_progress(monkeypatch):
    """The concrete difference from ``SubprocessGoalRunner``: a message that arrives WHILE the deep
    turn is running is injected into that turn, not held until the next goal-loop attempt."""
    conn = FakeConn(updates=[message_chunk("working")], wait_for_steering=True)
    install_fake_connection(monkeypatch, conn)

    inbox = InMemoryInbox()
    inbox.push("conv-1", "also update the changelog")

    events = []
    runner = acp.AcpDeepRunner(config(steering_inbox=inbox, steering_conversation_id="conv-1"))
    result = runner.run_goal(goal="g", brief="b", emit=events.append)

    assert conn.steered == ["also update the changelog"]     # it went over the wire, mid-turn
    assert result.met is True
    steer_events = [e for e in events if e.data["phase"] == acp.PHASE_STEER]
    assert len(steer_events) == 1
    assert "also update the changelog" in steer_events[0].text
    assert inbox.drain("conv-1") == []                       # consumed, not left behind


def test_steer_can_be_called_directly_from_another_thread(monkeypatch):
    """The route for an interface that already holds the message and wants no inbox wiring."""
    import threading

    conn = FakeConn(updates=[message_chunk("working")], wait_for_steering=True)
    install_fake_connection(monkeypatch, conn)
    runner = acp.AcpDeepRunner(config())

    def push_when_live():
        for _ in range(400):
            if runner.active_runs():
                break
            import time
            time.sleep(0.01)
        runner.steer("stop and summarize instead")

    pusher = threading.Thread(target=push_when_live, daemon=True)
    pusher.start()
    result = runner.run_goal(goal="g", brief="b", run_id="run-1")
    pusher.join(timeout=5)

    assert conn.steered == ["stop and summarize instead"]
    assert result.met is True
    assert runner.active_runs() == []          # the run deregisters when it finishes


def test_a_message_that_missed_the_turn_goes_back_to_the_queue(monkeypatch):
    """``promptRequired`` means the turn had already settled. The message must be requeued for the
    orchestrator's own between-attempts drain, never dropped, and never re-offered in a loop."""
    conn = FakeConn(updates=[message_chunk("done")], wait_for_steering=True,
                    steering_outcome="promptRequired")
    install_fake_connection(monkeypatch, conn)

    inbox = InMemoryInbox()
    inbox.push("conv-1", "one more thing")
    runner = acp.AcpDeepRunner(config(steering_inbox=inbox, steering_conversation_id="conv-1"))
    result = runner.run_goal(goal="g", brief="b")

    assert result.met is True
    assert len(conn.steered) == 1                       # tried exactly once, no retry storm
    assert inbox.drain("conv-1") == ["one more thing"]  # still available to the next attempt


def test_steering_is_not_attempted_when_the_agent_does_not_advertise_it(monkeypatch):
    conn = FakeConn(updates=[message_chunk("done")], steering=False)
    install_fake_connection(monkeypatch, conn)
    inbox = InMemoryInbox()
    inbox.push("conv-1", "later then")
    runner = acp.AcpDeepRunner(config(steering_inbox=inbox, steering_conversation_id="conv-1"))
    runner.run_goal(goal="g", brief="b")
    assert conn.steered == []
    assert inbox.drain("conv-1") == ["later then"]


# --- 6. graceful degradation -------------------------------------------------------------------

def test_a_missing_client_package_is_a_reported_failure_not_a_crash(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(),
                            spawn_error=acp.AcpUnavailable("the ACP client package is not installed"))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "not installed" in (result.error or "")


def test_a_missing_agent_binary_is_a_reported_failure(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(), spawn_error=FileNotFoundError())
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "not found" in (result.error or "")


def test_a_failed_handshake_is_a_reported_failure(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(fail_initialize=RuntimeError("bad protocol")))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "bad protocol" in (result.error or "")


def test_a_session_created_without_an_id_is_a_reported_failure(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(session_id=""))
    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "no session" in (result.error or "")


def test_a_broken_progress_sink_never_breaks_the_run(monkeypatch):
    install_fake_connection(monkeypatch, FakeConn(updates=[message_chunk("fine")]))

    def exploding_sink(event):
        raise RuntimeError("the websocket dropped")

    result = acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b", emit=exploding_sink)
    assert result.met is True and result.output == "fine"


def test_the_turn_timeout_cancels_the_session_and_fails_hard(monkeypatch):
    class SlowConn(FakeConn):
        async def prompt(self, *, session_id, prompt):
            await asyncio.sleep(30)
            return ns(stop_reason="end_turn", usage=None)

    conn = SlowConn(updates=[])
    install_fake_connection(monkeypatch, conn)
    result = acp.AcpDeepRunner(config(timeout_seconds=0.2)).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "wall-clock timeout" in (result.error or "")
    assert conn.cancelled is True


# --- 7. Node / agent resolution ----------------------------------------------------------------

def fake_node(monkeypatch, version: str):
    def fake_run(cmd, **kwargs):
        assert cmd[1] == "--version"
        return subprocess.CompletedProcess(cmd, 0, stdout=version.encode(), stderr=b"")
    monkeypatch.setattr(acp.subprocess, "run", fake_run)


def test_a_js_entry_point_is_launched_under_the_configured_node(monkeypatch, tmp_path):
    entry = tmp_path / "index.js"
    entry.write_text("// agent\n")
    fake_node(monkeypatch, "v22.23.2")
    argv, error = acp.build_agent_argv(agent_command=str(entry), agent_args=["--verbose"],
                                       node_path="/opt/node22/bin/node")
    assert error is None
    assert argv == ["/opt/node22/bin/node", str(entry), "--verbose"]


def test_a_too_old_node_fails_loudly_and_names_the_env_var(monkeypatch, tmp_path):
    entry = tmp_path / "index.js"
    entry.write_text("// agent\n")
    fake_node(monkeypatch, "v20.19.4")
    argv, error = acp.build_agent_argv(agent_command=str(entry), agent_args=[],
                                       node_path="/usr/bin/node")
    assert argv == []
    assert "Node >= 22" in error and "v20" in error
    assert acp.QAR_ACP_NODE_PATH_ENV_VAR in error


def test_a_missing_agent_program_names_what_to_install(monkeypatch):
    monkeypatch.delenv(acp.QAR_ACP_AGENT_COMMAND_ENV_VAR, raising=False)
    monkeypatch.setattr(acp.shutil, "which", lambda name: None)
    argv, error = acp.build_agent_argv(agent_command=None, agent_args=[], node_path=None)
    assert argv == []
    assert "claude-agent-acp" in error and acp.QAR_ACP_AGENT_COMMAND_ENV_VAR in error


def test_the_node_path_env_var_is_the_fallback(monkeypatch):
    monkeypatch.setenv(acp.QAR_ACP_NODE_PATH_ENV_VAR, "/opt/node22/bin/node")
    assert acp.resolve_node_binary(None) == "/opt/node22/bin/node"
    assert acp.resolve_node_binary("/explicit/node") == "/explicit/node"   # config wins


def test_the_agent_command_env_var_is_the_fallback(monkeypatch):
    monkeypatch.setenv(acp.QAR_ACP_AGENT_COMMAND_ENV_VAR, "/opt/acp/index.js")
    assert acp.resolve_agent_entry(None) == "/opt/acp/index.js"


def test_run_goal_reports_the_resolution_error_instead_of_spawning(monkeypatch):
    monkeypatch.delenv(acp.QAR_ACP_AGENT_COMMAND_ENV_VAR, raising=False)
    monkeypatch.setattr(acp.shutil, "which", lambda name: None)
    result = acp.AcpDeepRunner(acp.AcpConfig(working_dir="/w")).run_goal(goal="g", brief="b")
    assert result.met is False
    assert "claude-agent-acp" in (result.error or "")


# --- 8. session configuration ------------------------------------------------------------------

def test_the_model_family_is_derived_from_the_resolved_model_id():
    assert acp.acp_model_family("claude-sonnet-4-5-20250929") == "sonnet"
    assert acp.acp_model_family("opus") == "opus"
    assert acp.acp_model_family("gemini-3.5-flash") is None      # not a Claude family: leave default
    assert acp.acp_model_family(None) is None


def test_only_options_the_session_advertised_are_set(monkeypatch):
    advertised = [{"id": "model", "options": [{"value": "sonnet"}, {"value": "opus"}]}]
    conn = FakeConn(updates=[message_chunk("ok")], config_options=advertised,
                    modes=ns(available_modes=[ns(id="bypassPermissions")]))
    install_fake_connection(monkeypatch, conn)
    acp.AcpDeepRunner(config(effort="high", permission_mode="bypassPermissions")).run_goal(
        goal="g", brief="b", model="claude-sonnet-4-5-20250929")

    assert conn.set_options == [("model", "sonnet")]     # effort was NOT advertised, so not set
    assert conn.set_modes == ["bypassPermissions"]


def test_a_non_claude_model_leaves_the_agent_on_its_default(monkeypatch):
    advertised = [{"id": "model", "options": [{"value": "sonnet"}]}]
    conn = FakeConn(updates=[message_chunk("ok")], config_options=advertised)
    install_fake_connection(monkeypatch, conn)
    acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b", model="gemini-3.5-flash")
    assert conn.set_options == []


def test_the_composed_prompt_carries_the_preamble_the_goal_and_the_brief(monkeypatch):
    conn = FakeConn(updates=[message_chunk("ok")])
    install_fake_connection(monkeypatch, conn)
    acp.AcpDeepRunner(config(context_preamble="You are the team's AI.")).run_goal(
        goal="all tests pass", brief="fix the failing test")
    sent = conn.prompt_sent[0]["text"]
    assert sent.startswith("You are the team's AI.")
    assert "fix the failing test" in sent and "all tests pass" in sent


def test_a_per_call_working_dir_overrides_the_configured_one(monkeypatch):
    conn = FakeConn(updates=[message_chunk("ok")])
    install_fake_connection(monkeypatch, conn)
    acp.AcpDeepRunner(config()).run_goal(goal="g", brief="b", working_dir="/quests/alpha")
    assert conn.cwd == "/quests/alpha"


def test_the_child_env_does_not_inherit_our_own_claude_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    env = acp.AcpDeepRunner(config()).build_env()
    assert "CLAUDECODE" not in env and "ANTHROPIC_API_KEY" not in env
