"""CURRENT (opt-in): ``adapters.acp_deep_runner.AcpDeepRunner``, run through ``run_goal`` with its one SDK/process
seam (``open_agent_connection``) replaced by a scripted ACP connection (message chunks, a stop reason, a
missing agent, a crash). Only that seam is faked. Zero model calls."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from deep_runner.support import normalise
from quest_ai_runner.adapters import acp_deep_runner as acp
from hubofhubs.deep import acp_runner as hub_acp

STOP_FOR_EXIT = {0: "end_turn"}


class ScriptedConn:
    """A stand-in for the ACP client connection driven by the scenario."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.client = None

    async def initialize(self, **kw):
        return SimpleNamespace(field_meta={})

    async def new_session(self, **kw):
        return SimpleNamespace(session_id="sess-1", config_options=[], modes=None)

    async def set_config_option(self, **kw):
        return None

    async def set_session_mode(self, **kw):
        return None

    async def prompt(self, *, session_id, prompt):
        if self.scenario.get("crash"):
            raise RuntimeError("worker died")
        output = self.scenario.get("output", "")
        if output:
            await self.client.session_update(session_id, {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": output}, "messageId": "m1"})
        stop = self.scenario.get("stop") or STOP_FOR_EXIT.get(self.scenario.get("exit", 0), "max_tokens")
        return SimpleNamespace(stop_reason=stop, usage=None)

    async def cancel(self, **kw):
        return None

    async def close_session(self, **kw):
        return None


def build(meter):  # noqa: ARG001
    def work(kind, args):
        if kind != "run_worker":
            raise ValueError(kind)
        scenario = args["scenario"]

        @asynccontextmanager
        async def fake_open(client, argv, *, env, cwd):  # noqa: ARG001
            if scenario.get("spawn_error") == "missing":
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            conn = ScriptedConn(scenario)
            conn.client = client
            client.on_connect(conn)
            yield conn, None

        real = hub_acp.open_agent_connection
        hub_acp.open_agent_connection = fake_open
        try:
            runner = acp.AcpDeepRunner(acp.AcpConfig(
                working_dir="/work", agent_command="/opt/acp/claude-agent-acp",
                steering_poll_seconds=0.01, heartbeat_seconds=1000.0, timeout_seconds=20.0))
            try:
                result = runner.run_goal(goal="update the docs", brief="do it")
            except Exception:  # noqa: BLE001
                return normalise(None, args.get("want"), raised=True)
        finally:
            hub_acp.open_agent_connection = real
        return normalise(result, args.get("want"))
    return work
