"""Wiring MCP writes into the deep-runner ladder, and the gate that keeps them off the read loop.

Mirrors ``test_fast_edit_ladder.py``'s two claims, for the MCP-write rung:

1. OPT-IN. A consumer that configures an ``MCPServerSpec`` with no ``writable_tools`` gets no MCP
   write capability anywhere. Write access appears only when a spec's ``writable_tools`` is set.

2. THE GATE. ``MCPWriteAdapter.write_operation`` is investigated (see ``mcp_write_runner.py``'s
   module docstring) to have exactly ONE reachable caller in this library:
   ``MCPOperationRunner.run_goal``, which itself is reachable ONLY through the orchestrator's
   deep-runner ladder -- i.e. only once the planner has already decided ``action: "deep"``. This
   file drives the REAL orchestrator loop (not a mock of it) to prove that a plain answer/gather
   turn can never reach a write, and that the SAME wiring genuinely does execute one once the
   planner actually escalates to "deep" (a positive control, so the negative result isn't just an
   unreachable/broken runner).

Offline: no MCP package, no subprocess, no network, no ``claude`` binary is ever run.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.mcp_client import MCPServerSpec
from quest_ai_runner.adapters.mcp_write_adapter import MCPWriteAdapter
from quest_ai_runner.adapters.mcp_write_runner import MCPOperationRunner
from quest_ai_runner.config import RunnerConfig, build_orchestrator, resolve_mcp_write_runners
from quest_ai_runner.core.adapters import WriteResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, PlanDecision

from .conftest import StubProvider, StubRetrieval


class FakeWriter:
    """An OperationWriter double with a call spy -- fails the calling test's assertion if
    write_operation is ever invoked when it shouldn't be."""

    def __init__(self, catalog: Optional[List[Dict[str, Any]]] = None,
                 result: Optional[WriteResult] = None):
        self.catalog = catalog or [{
            "name": "srv:comment", "description": "Post a comment", "input_schema": {"type": "object"},
        }]
        self.result = result or WriteResult(ok=True, rel_path="srv:comment",
                                            detail={"tool": "comment", "args": {}, "executed": True})
        self.write_calls: List[tuple] = []

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        return list(self.catalog)

    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult:
        self.write_calls.append((name, args))
        return self.result


def _cfg(**kwargs) -> RunnerConfig:
    return RunnerConfig(retrieval=StubRetrieval({"README.md": "hi"}),
                        model_provider=StubProvider([]), **kwargs)


def _mcp_spec(**overrides: Any) -> MCPServerSpec:
    base: Dict[str, Any] = dict(alias="srv", transport="stdio", command="fake")
    base.update(overrides)
    return MCPServerSpec(**base)


# --- 1. opt-in: no writable_tools means no MCP write capability anywhere -------------------------

def test_mcp_servers_with_no_writable_tools_yields_no_write_runner():
    cfg = _cfg(mcp_servers=[_mcp_spec(allowed_tools=["search"])])  # read-only spec
    assert resolve_mcp_write_runners(cfg) == []


def test_no_mcp_servers_at_all_yields_no_write_runner():
    cfg = _cfg()
    assert resolve_mcp_write_runners(cfg) == []


def test_writable_tools_spec_produces_one_mcp_operation_runner():
    cfg = _cfg(mcp_servers=[_mcp_spec(writable_tools=["comment"])])
    runners = resolve_mcp_write_runners(cfg)
    assert len(runners) == 1
    assert isinstance(runners[0], MCPOperationRunner)
    assert isinstance(runners[0].writer, MCPWriteAdapter)
    assert runners[0].writer.alias == "srv"
    assert runners[0].writer.writable_tools == ["comment"]


def test_each_writable_spec_gets_its_own_runner_and_alias():
    cfg = _cfg(mcp_servers=[
        _mcp_spec(alias="issues", writable_tools=["create"]),
        _mcp_spec(alias="chat", writable_tools=["send"]),
        _mcp_spec(alias="readonly", allowed_tools=["search"]),  # no writable_tools: skipped
    ])
    runners = resolve_mcp_write_runners(cfg)
    aliases = sorted(r.writer.alias for r in runners)
    assert aliases == ["chat", "issues"]


def test_no_model_provider_means_no_write_runner():
    cfg = RunnerConfig(retrieval=StubRetrieval(), mcp_servers=[_mcp_spec(writable_tools=["comment"])])
    assert resolve_mcp_write_runners(cfg) == []


def test_read_allowlist_alone_never_implies_write_capability():
    """A spec allowlisted for READ (allowed_tools) but with no writable_tools produces zero write
    runners -- the two allowlists are genuinely independent."""
    cfg = _cfg(mcp_servers=[_mcp_spec(alias="srv", allowed_tools=["comment", "delete_everything"])])
    assert resolve_mcp_write_runners(cfg) == []


# --- 2. the gate: a plain answer/gather turn can never reach a write -----------------------------

def test_plain_answer_turn_never_reaches_write_operation():
    """THE critical proof: driving the REAL orchestrator loop through a plain answer (no 'deep')
    must never call write_operation, even with a write-capable runner sitting in the ladder."""
    writer = FakeWriter()
    write_runner = MCPOperationRunner(provider=StubProvider([]), writer=writer)
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "just answer"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval({"README.md": "hi"}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner_ladder=[write_runner])

    res = orch.run("what's in the readme?")

    assert res.kind == "answer"
    assert writer.write_calls == [], "a plain answer turn must never execute an MCP write"


def test_plain_read_turn_never_reaches_write_operation():
    writer = FakeWriter()
    write_runner = MCPOperationRunner(provider=StubProvider([]), writer=writer)
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "model_tier": "sonnet",
         "rationale": "need the doc"},
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval({"README.md": "GROUNDING: hi"}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner_ladder=[write_runner])

    res = orch.run("what's in the readme?")

    assert res.kind == "answer"
    assert writer.write_calls == []


def test_positive_control_deep_action_does_reach_the_write_runner():
    """Same wiring as the negative tests above, but driven through ``_run_deep`` (the planner's
    OWN structured "deep" decision) -- proving the ladder genuinely CAN execute a write when the
    real gate is taken, so the negative results above are not just a broken/unreachable runner."""
    writer = FakeWriter()
    write_runner = MCPOperationRunner(
        provider=StubProvider([{"operation": "srv:comment", "args": {"body": "hi"}}]),
        writer=writer,
    )
    provider = StubProvider([])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), deep_runner_ladder=[write_runner])
    orch._verify_goal = lambda goal, brief, output, **kwargs: ({"met": True}, None)

    res = orch._run_deep(PlanDecision(action="deep", goal="post a comment saying hi",
                                      deep_brief="post a comment saying hi"),
                         "post a comment saying hi", "sonnet")

    assert res.deep_results[0].met is True
    assert writer.write_calls == [("srv:comment", {"body": "hi"})]


# --- build_orchestrator wiring end to end ---------------------------------------------------------

def test_build_orchestrator_wires_the_write_runner_into_the_ladder(monkeypatch):
    monkeypatch.setenv("QAR_CLAUDE_PATH", "qar-test-no-such-claude-binary")
    cfg = _cfg(mcp_servers=[_mcp_spec(writable_tools=["comment"])])
    orch = build_orchestrator(cfg)
    assert any(isinstance(r, MCPOperationRunner) for r in orch.deep_runner_ladder)


def test_build_orchestrator_default_consumer_has_no_write_runner(monkeypatch):
    monkeypatch.setenv("QAR_CLAUDE_PATH", "qar-test-no-such-claude-binary")
    orch = build_orchestrator(_cfg())
    assert not any(isinstance(r, MCPOperationRunner) for r in orch.deep_runner_ladder)
