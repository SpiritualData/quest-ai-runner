"""Shared stub adapters for the tests — no network, no API key, no real model.

These are the minimal implementations of the four core interfaces that let us drive the brain
and the runner deterministically.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import (
    DeepResult,
    Escalation,
    Observation,
)


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
        self.last_plan_prompt: str = ""
        self.plan_prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.plan_calls += 1
        self.last_plan_prompt = prompt
        self.plan_prompts.append(prompt)
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_calls += 1
        self.last_answer_messages = messages
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
    def __init__(self, met: bool = True, output: str = "deep done", error: str | None = None):
        self._met = met
        self._output = output
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model, "max_turns": max_turns})
        return DeepResult(met=self._met, output=self._output, error=self._error)


class StubEscalation:
    def __init__(self, decision_id: str = "dec_123"):
        self._id = decision_id
        self.raised: List[Escalation] = []

    def escalate(self, escalation: Escalation) -> str:
        self.raised.append(escalation)
        return self._id
