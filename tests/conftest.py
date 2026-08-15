"""Shared stub adapters for the tests — no network, no API key, no real model.

These are the minimal implementations of the four core interfaces that let us drive the brain
and the runner deterministically.
"""
from typing import Any, Dict, List

import pytest

from quest_ai_runner.config import shutdown_background_index
from quest_ai_runner.core.adapters import (
    DeepResult,
    Escalation,
    Observation,
)


@pytest.fixture(autouse=True)
def deep_runner_default_is_inert_in_tests(monkeypatch):
    """The auto-built default deep runner must never resolve to a REAL worker during the suite.

    ``RunnerConfig.deep_runner`` left unset means "build the default SubprocessGoalRunner pointed
    at ``claude`` on PATH" (see ``config.resolve_deep_runner``). That is the right product default
    and the wrong test default twice over: the suite must be offline and hermetic (a turn routed to
    deep would otherwise SPAWN Claude Code for real), and its results must not depend on whether
    the developer's machine happens to have Claude Code installed.

    Pointing ``QAR_CLAUDE_PATH`` at a binary that cannot exist makes the resolution take its
    documented graceful-degradation branch (warn, no runner) uniformly everywhere. Tests that care
    about the resolution itself override this with their own ``monkeypatch.setenv`` /
    ``shutil.which`` patching (see ``tests/test_deep_runner_default.py``), which is applied after
    this fixture and therefore wins.
    """
    monkeypatch.setenv("QAR_CLAUDE_PATH", "qar-test-no-such-claude-binary")


@pytest.fixture(autouse=True)
def no_background_index_survives_a_test():
    """No context-index thread may outlive the test that started it.

    ``config._bootstrap_if_needed`` indexes the corpus on a background thread, and that pass shells
    out to ``git hash-object`` per file. A thread that outlived its test used to land those
    subprocess calls inside a LATER test that had patched ``subprocess`` or the environment, which
    failed a different test on each run (test_runner.py's explicit-session-id test and
    test_vector_context.py's backend-env switch were the usual victims). Non-determinism like that
    is where real regressions hide.

    The library owns those threads now (``shutdown_background_index`` closes the store and joins
    them), so this fixture is simply that call at teardown: the guarantee is enforced in the library,
    and asserted here for every test at once.
    """
    yield
    shutdown_background_index(timeout=10.0)


class StubProvider:
    """A ModelProvider whose plan() replays a scripted list of decisions and whose answer()
    echoes the gathered grounding so tests can assert it answered from real content."""

    def __init__(self, decisions: List[Dict[str, Any]], answer_text: str = "STUB ANSWER",
                 models: List[str] | None = None):
        self._decisions = list(decisions)
        self._answer_text = answer_text
        self._models = models or ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
        self.plan_calls = 0
        self.answer_calls = 0
        self.last_answer_messages: List[Dict[str, str]] = []
        # EVERY answer() call's messages, in order (the last one alone is not enough when a turn
        # answers, then re-synthesizes: a test asserting what a turn did or did not claim has to
        # see all of them).
        self.all_answer_messages: List[List[Dict[str, str]]] = []
        # Every system prompt an answer() call was given, in order, so a test can assert the
        # reply-voice contract actually reaches the model (None = no system prompt was passed).
        self.answer_systems: List[Any] = []
        self.last_plan_prompt: str = ""
        self.plan_prompts: List[str] = []
        # Every tool schema a plan() call was given, in order, so a test can assert which fields
        # the planner was actually allowed to fill (e.g. the opt-in `mode_signal` field).
        self.plan_tool_schemas: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.plan_calls += 1
        self.last_plan_prompt = prompt
        self.plan_prompts.append(prompt)
        self.plan_tool_schemas.append(tool_schema)
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_calls += 1
        self.last_answer_messages = messages
        self.all_answer_messages.append(list(messages))
        self.answer_systems.append(system)
        joined = "\n".join(m["content"] for m in messages)
        return f"{self._answer_text} [grounded_on:{'README' in joined or 'GROUNDING' in joined}]"

    def list_models(self) -> List[str]:
        return self._models


class StubRetrieval:
    """A RetrievalAdapter backed by an in-memory file map."""

    def __init__(self, files: Dict[str, str] | None = None):
        self.files = files or {}
        self.read_calls: List[str] = []
        self.grep_calls: List[str] = []

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None, max_bytes=None):
        self.read_calls.append(rel_path)
        if rel_path not in self.files:
            return Observation(kind="error", rel_path=rel_path, error="not found")
        return Observation(kind="read", rel_path=rel_path, locator="head", text=self.files[rel_path])

    def grep(self, pattern, *, scope=None, max_hits=None):
        self.grep_calls.append(pattern)
        hits = []
        for rp, content in self.files.items():
            for i, line in enumerate(content.splitlines(), start=1):
                if pattern.lower() in line.lower():
                    hits.append({"rel_path": rp, "line_no": i, "line": line})
        return Observation(kind="grep", pattern=pattern, scope=scope, hits=hits)

    def query(self, spec):
        return Observation(kind="error", error="query unsupported in stub")


class StubDeepRunner:
    def __init__(self, met: bool = True, output: str = "deep done", error: str | None = None,
                 decision_id: str | None = None, deferred: bool = False):
        self._met = met
        self._output = output
        self._error = error
        self._decision_id = decision_id
        self._deferred = deferred
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model, "max_turns": max_turns})
        return DeepResult(met=self._met, output=self._output, error=self._error,
                          decision_id=self._decision_id, deferred=self._deferred)


class StubEscalation:
    def __init__(self, decision_id: str = "dec_123"):
        self._id = decision_id
        self.raised: List[Escalation] = []

    def escalate(self, escalation: Escalation) -> str:
        self.raised.append(escalation)
        return self._id
