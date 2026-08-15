"""MCPOperationRunner — one model call, executed in process, through an OperationWriter.

Mirrors ``test_fast_edit_runner.py``'s rigor for the MCP-write analogue of ``FastEditRunner``:

  * a forced structured decision (never free-text keyword scanning) picks an operation + args from
    the writer's own discovered catalog;
  * a successful execution reports met=True with the tool/args in the summary and future_context;
  * declining (empty catalog, or the model's own explicit "" decline) reports met=False without
    ever calling write_operation, so the ladder escalates rather than the runner inventing work;
  * a write that the writer refuses/fails reports met=False, never raises;
  * a planner call that raises is caught and reported met=False, never raises.

Fully offline: the provider is a scripted stub, the writer is an in-memory fake -- no MCP, no
subprocess, no network.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.mcp_write_runner import MCPOperationConfig, MCPOperationRunner
from quest_ai_runner.core.adapters import WriteResult


class ScriptedPlanProvider:
    """A ModelProvider whose ``plan`` replays scripted decisions and records what it was asked."""

    def __init__(self, decisions: List[Dict[str, Any]]):
        self.decisions = list(decisions)
        self.prompts: List[str] = []
        self.tool_schemas: List[Dict[str, Any]] = []
        self.models: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.prompts.append(prompt)
        self.tool_schemas.append(tool_schema)
        self.models.append(model)
        if not self.decisions:
            raise AssertionError("no more scripted decisions")
        return self.decisions.pop(0)

    def answer(self, messages, *, model, system=None, layers=None) -> str:
        raise AssertionError("the mcp operation runner must not call answer()")

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6", "claude-opus-4-8"]


class RaisingPlanProvider(ScriptedPlanProvider):
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("provider is down")


class FakeWriter:
    """An OperationWriter double: scripted catalog + scripted write_operation result, with a
    call spy so a test can prove write_operation was (or was NOT) invoked."""

    def __init__(self, catalog: Optional[List[Dict[str, Any]]] = None,
                 result: Optional[WriteResult] = None):
        self.catalog = catalog or []
        self.result = result or WriteResult(ok=True, rel_path="srv:op")
        self.write_calls: List[tuple] = []

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        return list(self.catalog)

    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult:
        self.write_calls.append((name, args))
        return self.result


CATALOG = [{
    "name": "srv:comment", "description": "Post a comment",
    "input_schema": {"type": "object", "properties": {"body": {"type": "string"}}},
}]


# --- declining -------------------------------------------------------------------------------

def test_empty_catalog_declines_without_calling_the_planner():
    provider = ScriptedPlanProvider([])
    writer = FakeWriter(catalog=[])
    runner = MCPOperationRunner(provider=provider, writer=writer)

    res = runner.run_goal(goal="post a comment", brief="post a comment saying hi")

    assert res.met is False
    assert provider.prompts == []  # never even asked the model
    assert writer.write_calls == []


def test_model_decline_sentinel_reports_not_met_without_writing():
    provider = ScriptedPlanProvider([{"operation": "", "args": {}}])
    writer = FakeWriter(catalog=CATALOG)
    runner = MCPOperationRunner(provider=provider, writer=writer)

    res = runner.run_goal(goal="do something unrelated", brief="do something unrelated")

    assert res.met is False
    assert "declined" in res.error
    assert writer.write_calls == []


# --- successful execution ---------------------------------------------------------------------

def test_chosen_operation_is_executed_and_reported():
    provider = ScriptedPlanProvider([{"operation": "srv:comment", "args": {"body": "hello"}}])
    writer = FakeWriter(catalog=CATALOG,
                        result=WriteResult(ok=True, rel_path="srv:comment",
                                           detail={"tool": "comment", "args": {"body": "hello"},
                                                   "executed": True, "content": "posted"}))
    runner = MCPOperationRunner(provider=provider, writer=writer)

    res = runner.run_goal(goal="post a comment saying hello", brief="post a comment saying hello")

    assert res.met is True
    assert writer.write_calls == [("srv:comment", {"body": "hello"})]
    assert "srv:comment" in res.output
    assert "hello" in res.future_context


def test_catalog_schema_reaches_the_planner_prompt():
    """The model is not asked to invent an operation -- the catalog's own schema is in the
    prompt it plans against."""
    provider = ScriptedPlanProvider([{"operation": "srv:comment", "args": {"body": "hi"}}])
    writer = FakeWriter(catalog=CATALOG)
    runner = MCPOperationRunner(provider=provider, writer=writer)

    runner.run_goal(goal="post a comment", brief="post a comment")

    assert "srv:comment" in provider.prompts[0]
    assert "Post a comment" in provider.prompts[0]
    # the forced-schema decision offers exactly the catalog's operation names + the decline value
    assert provider.tool_schemas[0]["input_schema"]["properties"]["operation"]["enum"] == ["srv:comment", ""]


# --- refused / failed write ----------------------------------------------------------------------

def test_refused_write_reports_not_met_never_raises():
    provider = ScriptedPlanProvider([{"operation": "srv:comment", "args": {"body": "x"}}])
    writer = FakeWriter(catalog=CATALOG,
                        result=WriteResult(ok=False, rel_path="srv:comment", error="rate limited"))
    runner = MCPOperationRunner(provider=provider, writer=writer)

    res = runner.run_goal(goal="post a comment", brief="post a comment")

    assert res.met is False
    assert "rate limited" in res.error


# --- planner failure -----------------------------------------------------------------------------

def test_planner_call_raising_is_caught_and_reported_not_met():
    provider = RaisingPlanProvider([])
    writer = FakeWriter(catalog=CATALOG)
    runner = MCPOperationRunner(provider=provider, writer=writer)

    res = runner.run_goal(goal="post a comment", brief="post a comment")  # must not raise

    assert res.met is False
    assert "provider is down" in res.error
    assert writer.write_calls == []


# --- config -----------------------------------------------------------------------------------

def test_default_config_uses_quality_tier():
    assert MCPOperationConfig().tier == "quality"
