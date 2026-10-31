"""CURRENT: the production deterministic rules, called directly. ``parse_deep_review``,
``SubprocessGoalRunner.review_liveness`` (with a scripted reviewer and no session file), the escalation,
resume and envelope helpers, ``compose_goal_prompt``, ``GoalRunner.run``'s met-vs-limit normalisation, and
the real Orchestrator goal loop with a scripted runner and verifier. The environment is pinned (no standing
instructions file) so the prompt is the library's own. Zero model calls; no worker is started."""
import contextlib
import os
import tempfile
from pathlib import Path

from quest_ai_runner.core import goal_runner_contract as grc
from hubofhubs.ports.deep import DeepResult
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig


@contextlib.contextmanager
def pinned_env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ScriptedRunner:
    """A DeepRunner returning scripted results in order, recording its calls."""

    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None):  # noqa: ARG002
        self.calls += 1
        item = self.results.pop(0) if self.results else DeepResult(met=True, output="done")
        if item == "__raise__":
            raise RuntimeError("worker crashed")
        return item


class PlainRunner:
    """The oldest DeepRunner signature: no optional keyword the wrapper could forward."""

    def __init__(self):
        self.kwargs = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None):  # noqa: ARG002
        self.kwargs = sorted(["goal", "brief", "model", "max_turns"])
        return DeepResult(met=True, output="ok")


class FullRunner:
    def __init__(self):
        self.got = {}

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None, working_dir=None,
                 resume_session_id=None):  # noqa: ARG002
        self.got = dict(context_preamble=context_preamble, working_dir=working_dir,
                        resume_session_id=resume_session_id)
        return DeepResult(met=True, output="ok")


class ScriptedVerifier:
    """plan() answers the goal-verdict schema from a list; anything else is a plain answer plan."""

    def __init__(self, verdicts):
        self.verdicts, self.verify_calls = list(verdicts), 0

    def plan(self, prompt, *, model, tool_schema):  # noqa: ARG002
        if tool_schema.get("name") == "goal_verdict":
            self.verify_calls += 1
            return self.verdicts.pop(0) if self.verdicts else {"met": True}
        return {"action": "deep", "goal": "Report the status", "rationale": "deep",
                "deep_subtasks": [{"goal": "Report the job status", "brief": "look it up"}]}

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return "ANSWER"

    def list_models(self):
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class NoSources:
    def read_section(self, *a, **k):  # noqa: ARG002
        raise RuntimeError("no sources in this eval")

    def grep(self, *a, **k):  # noqa: ARG002
        raise RuntimeError("no sources in this eval")

    def query(self, spec):  # noqa: ARG002
        raise RuntimeError("no sources in this eval")


def _result(spec):
    return spec if spec == "__raise__" else DeepResult(**spec)


def build(meter):  # noqa: ARG001
    def contract(kind, args):
        if kind == "review_parse":
            v = grc.parse_deep_review(args["text"])
            return {"stop": None, "reason": ""} if v is None else {"stop": v[0], "reason": v[1]}
        if kind == "review_fail_open":
            def reviewer(prompt):
                if args["reply"] == "__raise__":
                    raise RuntimeError("reviewer down")
                return args["reply"]
            runner = SubprocessGoalRunner(SubprocessConfig(working_dir=tempfile.gettempdir(), claude_path="claude",
                                                           reviewer=reviewer))
            stop, reason, _ = runner.review_liveness(tempfile.gettempdir(), None, 600, None, "eval")
            return {"stop": stop, "reason": reason}
        if kind == "escalation_id":
            return {"id": grc.extract_escalation_id(args["output"])}
        if kind == "resume_missing":
            return {"missing": grc.resume_target_missing(args["returncode"], args["output"], args["stderr"])}
        if kind == "envelope_no_work":
            return {"no_work": grc.envelope_reports_no_work(args["raw"])}
        if kind == "worker_output":
            text, tokens, cost, is_error, subtype = grc._parse_worker_output(args["raw"])
            return dict(text=text, tokens=tokens, cost=cost, is_error=is_error, subtype=subtype)
        if kind == "goal_prompt":
            with pinned_env(QAR_DEEP_INSTRUCTIONS_FILES=""):
                text = grc.compose_goal_prompt(args["goal"], args["brief"], preamble=args.get("preamble", ""),
                                               budget_tokens=args.get("budget_tokens"))
            return {"text": text}
        if kind == "goal_contract":
            res = grc.GoalRunner(ScriptedRunner([_result(args["result"])])).run(goal="g", brief="b")
            return dict(met=res.met, error=res.error, decision_id=res.decision_id)
        if kind == "goal_kwargs":
            runner = PlainRunner() if args["runner"] == "plain" else FullRunner()
            grc.GoalRunner(runner).run(goal="g", brief="b", context_preamble="P", working_dir="/w",
                                       resume_session_id="s1")
            forwarded = [k for k, v in getattr(runner, "got", {}).items() if v is not None]
            return {"forwarded": sorted(forwarded)}
        if kind == "goal_loop":
            runner = ScriptedRunner([_result(r) for r in args["runs"]])
            provider = ScriptedVerifier(args["verdicts"])
            with pinned_env(QAR_DEEP_INSTRUCTIONS_FILES=""):
                orch = Orchestrator(retrieval=NoSources(), provider=provider, registry=ModelRegistry(provider),
                                    deep_runner=runner,
                                    config=OrchestratorConfig(deep_goal_max_iterations=args["max_iterations"]))
                res = orch.run("what is the job status?")
            final = res.deep_results[-1].met if res.deep_results else None
            return dict(runner_calls=runner.calls, verify_calls=provider.verify_calls, final_met=final)
        raise ValueError(kind)
    return contract
