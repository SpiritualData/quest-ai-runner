"""ABLATION of orchestrator_pre_route: the direct tool path ALONE. ONE model call picks the tool calls as JSON from the catalog's own schemas, and
the harness-owned registry executes them. No planner, no answer step, so it cannot read, answer or start a deep run: the upper bound on what the
direct path can do for a plain quest edit, not a replacement for the brain."""
import json
import os
import re

import common  # noqa: F401
from quest_ai_runner.core.tools import ToolContext

SYSTEM = ("You carry out ONE request on a user's quest by choosing tool calls. Reply with ONLY JSON "
          '{"calls": [{"name": ..., "args": {...}}]}. Use [] when the user asked for no change or only a read. '
          "Only write what the user asked for. Periods: week YYYY_W##, month YYYY_MM, day YYYY-MM-DD. "
          "Set user_asked_for_this_field true on every write. Today is 2026-10-14.")


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    provider = meter.wrap(ClaudeCliProvider())
    model = os.environ.get("QAR_EVAL_MODEL", "haiku")

    def run_operation(message, world):
        catalog = []
        for n in world.registry.names():
            s = world.registry.get(n)
            catalog.append({"name": n, "use": s.description[:200], "args": s.public_parameters().get("properties", {}),
                            "required": s.required()})
        experiments = [(e["id"], e["question"], e["status"]) for e in world.state["experiments"]]
        prompt = f"Current state: {world.state['current_state']}\nExperiments (id, question, status): {experiments}\nTools: {json.dumps(catalog)}\nRequest: {message}"
        text = provider.answer([{"role": "user", "content": prompt}], model=model, system=SYSTEM)
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            calls = (json.loads(m.group(0)).get("calls") if m else []) or []
        except json.JSONDecodeError:
            calls = []
        done = []
        for c in calls:
            r = world.registry.invoke(c.get("name"), c.get("args") or {}, ToolContext(quest_id=world.quest_id))
            done.append(f"{c.get('name')}: {'ok' if r.ok else r.text}")
        return {"reply": "; ".join(done) or "no change"}
    return run_operation
