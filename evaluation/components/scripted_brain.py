"""A real Orchestrator with a scripted model and no data sources, for the offline component evaluations that
must drive the live turn pipeline (guidance injection, mid-run inbox drain) without a model or a network.

``ScriptedProvider.plan`` replays a list of planner decisions (the ``decide`` tool; the overseer and the goal verifier get fixed benign answers) and records every planner prompt, so a case can
ask "did the guidance / the mid-run message reach the planner?". ``answer`` returns a fixed string. Nothing
here calls out of the process. ``on_plan`` is an optional hook called with the 0-based planner call index
before the decision is returned, which is how a case pushes a message "while the run is working"."""
import common  # noqa: F401  (puts the repo on sys.path)
from quest_ai_runner.core.adapters import Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig


class ScriptedProvider:
    def __init__(self, decisions, on_plan=None):
        self.decisions, self.on_plan = list(decisions), on_plan
        self.plan_prompts, self.plan_calls = [], 0

    def plan(self, prompt, *, model, tool_schema):  # noqa: ARG002
        name = tool_schema.get("name")
        if name == "goal_verdict":
            return {"met": True}
        if name != "decide":  # the overseer and any other side call: not a planner step, never consumes the script
            return {"signal": "proceed", "reason": ""}
        index = self.plan_calls
        self.plan_calls += 1
        self.plan_prompts.append(prompt)
        if self.on_plan is not None:
            self.on_plan(index)
        if self.decisions:
            return self.decisions.pop(0)
        return {"action": "answer", "rationale": "done", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return "SCRIPTED ANSWER"

    def list_models(self):
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class NoSources:
    """A RetrievalAdapter with nothing in it: every read is an error observation, never an exception."""

    def read_section(self, rel_path, **kwargs):  # noqa: ARG002
        return Observation(kind="error", rel_path=rel_path, error="no sources in this evaluation")

    def grep(self, pattern, **kwargs):  # noqa: ARG002
        return Observation(kind="grep", pattern=pattern, hits=[])

    def query(self, spec):  # noqa: ARG002
        return Observation(kind="error", error="no sources in this evaluation")


def build_brain(provider, **orchestrator_kwargs):
    return Orchestrator(retrieval=NoSources(), provider=provider, registry=ModelRegistry(provider),
                        config=orchestrator_kwargs.pop("config", OrchestratorConfig()), **orchestrator_kwargs)
