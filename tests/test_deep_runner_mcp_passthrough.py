"""Offline tests for the two small deep-runner MCP passthrough changes (Phase 1, item 4):

  * ``SubprocessGoalRunner`` (core/goal_runner.py) builds ``claude -p ... --mcp-config <path>``
    when ``SubprocessConfig.mcp_config_path`` is set, and omits the flag entirely when unset.
  * ``AcpDeepRunner`` (adapters/acp_deep_runner.py) passes ``AcpConfig.mcp_servers`` through to the
    ACP agent's ``session/new`` call instead of the previously hardcoded ``[]``.

No real ``claude`` binary or ACP agent is spawned in either test.
"""
from __future__ import annotations

import subprocess as _sp
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters import acp_deep_runner as acp
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner


# --- SubprocessGoalRunner: --mcp-config passthrough ---------------------------------------------

def _mock_popen(monkeypatch, captured: Dict[str, Any]):
    class _MockPopen:
        returncode = 0
        stdin = None

        def communicate(self, input=None, timeout=None):
            return (b'{"result": "did it"}', b"")

    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _MockPopen()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)


def test_mcp_config_path_is_passed_through_when_set(monkeypatch):
    captured: Dict[str, Any] = {}
    _mock_popen(monkeypatch, captured)
    runner = SubprocessGoalRunner(SubprocessConfig(
        working_dir="/w", claude_path="/usr/bin/claude", mcp_config_path="/etc/qar/mcp.json",
    ))
    runner.run_goal(goal="g", brief="b", max_turns=2)
    cmd = captured["cmd"]
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == "/etc/qar/mcp.json"


def test_mcp_config_path_omitted_when_unset(monkeypatch):
    captured: Dict[str, Any] = {}
    _mock_popen(monkeypatch, captured)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/w", claude_path="/usr/bin/claude"))
    runner.run_goal(goal="g", brief="b", max_turns=2)
    assert "--mcp-config" not in captured["cmd"]


# --- AcpDeepRunner: mcp_servers passthrough to session/new --------------------------------------

def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class FakeConn:
    """Minimal stand-in for ``acp.ClientSideConnection`` -- just enough to complete one turn and
    record what ``new_session`` was called with."""

    def __init__(self):
        self.client: Any = None
        self.new_session_kwargs: Dict[str, Any] = {}

    async def initialize(self, *, protocol_version: int, client_capabilities: Any = None, **kw: Any):
        return ns(field_meta={})

    async def new_session(self, *, cwd: str, mcp_servers: Any = None, **kw: Any):
        self.new_session_kwargs = {"cwd": cwd, "mcp_servers": mcp_servers}
        return ns(session_id="sess-1", config_options=[], modes=None)

    async def set_config_option(self, **kw: Any):
        pass

    async def set_session_mode(self, **kw: Any):
        pass

    async def prompt(self, *, session_id: str, prompt: List[Dict[str, Any]]):
        # A real turn always produces agent text; without this the runner's silent-no-op guard
        # (a clean end with nothing said means the goal never actually ran) would fail the result.
        await self.client.session_update(session_id, {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "done"},
            "messageId": "m1",
        })
        return ns(stop_reason="end_turn", usage=None)

    async def cancel(self, **kw: Any):
        pass

    async def close_session(self, **kw: Any):
        pass


def install_fake_connection(monkeypatch, conn: FakeConn):
    @asynccontextmanager
    async def fake_open(client, argv, *, env, cwd):
        conn.client = client
        client.on_connect(conn)
        yield conn, None

    monkeypatch.setattr(acp, "open_agent_connection", fake_open)


def acp_config(**overrides: Any) -> acp.AcpConfig:
    base: Dict[str, Any] = dict(
        working_dir="/work",
        agent_command="/opt/acp/claude-agent-acp",  # not a .js path -> no Node probe needed
        steering_poll_seconds=0.01,
        heartbeat_seconds=1000.0,
        timeout_seconds=20.0,
    )
    base.update(overrides)
    return acp.AcpConfig(**base)


def test_configured_mcp_servers_passed_to_new_session_instead_of_hardcoded_empty(monkeypatch):
    conn = FakeConn()
    install_fake_connection(monkeypatch, conn)
    configured = [{"name": "issues", "command": "issue-mcp-server"}]
    runner = acp.AcpDeepRunner(acp_config(mcp_servers=configured))
    result = runner.run_goal(goal="g", brief="b")
    assert result.met is True
    assert conn.new_session_kwargs["mcp_servers"] == configured


def test_default_mcp_servers_is_still_an_empty_list(monkeypatch):
    conn = FakeConn()
    install_fake_connection(monkeypatch, conn)
    runner = acp.AcpDeepRunner(acp_config())  # mcp_servers left at its default
    result = runner.run_goal(goal="g", brief="b")
    assert result.met is True
    assert conn.new_session_kwargs["mcp_servers"] == []
