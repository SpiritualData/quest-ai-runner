"""CURRENT: the real Orchestrator over the host's tool registry and retrieval adapter: reads go through the planner's
read action (a read already run this turn is not run again, a tool step resets that record), changes go through the
registry's declared operations (an identical successful call is not run twice). Zero model calls (scripted planner)."""
import common  # noqa: F401
from quest_ai_runner.core.adapters import Observation
from quest_ai_runner.core.tools import ToolRegistry, ToolResult, ToolSpec
from scripted_brain import ScriptedProvider, build_brain


class Account:
    def __init__(self):
        self.status = "draft"
        self.reads = 0  # reads of the account document that found it
        self.writes = []

    def read_section(self, rel_path, **kw):  # noqa: ARG002
        if rel_path != "account.md":
            return Observation(kind="error", rel_path=rel_path, error="not found")
        self.reads += 1
        return Observation(kind="read", rel_path=rel_path, locator="head", text=f"ACCOUNT status is {self.status}")

    def grep(self, pattern, **kw):  # noqa: ARG002
        return Observation(kind="grep", pattern=pattern, hits=[])

    def query(self, spec):  # noqa: ARG002
        return Observation(kind="error", error="query unsupported")


def build(meter):  # noqa: ARG001
    def run_script(case):
        account = Account()
        fails = {s["write"] for s in case["script"] if s.get("fails")}

        def handler(args, ctx):  # noqa: ARG001
            account.writes.append({"value": args["value"]})
            if args["value"] in fails:
                return ToolResult(ok=False, text="the operation failed")
            account.status = args["value"]
            return ToolResult(ok=True, text=f"status set to {args['value']}")

        registry = ToolRegistry([ToolSpec(
            name="set_status", description="set_status changes the account status", handler=handler,
            when_to_use="when the status must change", when_not_to_use="when only reading the status",
            mutates=True, keywords=("status",),
            parameters={"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}})])
        decisions = []
        for step in case["script"]:
            if step.get("read"):
                decisions.append({"action": "read", "reads": [{"rel_path": step.get("path", "account.md")}], "rationale": "read the account"})
            elif "write" in step:
                decisions.append({"action": "tool", "rationale": "change the status",
                                  "tool_calls": [{"name": "set_status", "args": {"value": step["write"]}}]})
            else:
                decisions.append({"action": "answer", "rationale": "done"})
        provider = ScriptedProvider(decisions)
        brain = build_brain(provider, tools=registry)
        brain.retrieval = account
        brain.cfg.overseer = False
        brain.run(case["message"])
        return {"handler_calls": account.writes, "reads": account.reads,
                "final_prompt": provider.plan_prompts[-1] if provider.plan_prompts else ""}
    return run_script
