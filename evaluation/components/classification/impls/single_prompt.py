"""ALTERNATIVE solution: one small direct prompt to the same model, no planner machinery, no tool
catalog beyond names. Shows a different framework is one new function behind the same signature."""
import json
import os
import re

import common  # noqa: F401  (sets sys.path for the repo)

SYSTEM = (
    "You route one message sent to an assistant inside a goal-tracking app. Reply with ONLY JSON "
    '{"route": "answer"|"tool"|"deep", "tool": <tool name or null>}.\n'
    "tool = the user asks to create/change/find something in their quest and one listed tool does it.\n"
    "deep = needs a computer: writing or running code, editing files, deploying, long research reports.\n"
    "answer = a question, advice, thanks, or the user says not to save anything.")


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    provider = meter.wrap(ClaudeCliProvider())
    model = os.environ.get("QAR_EVAL_MODEL", "haiku")

    def classify(message, context):
        prompt = f"Tools: {', '.join(context['tools'])}\nMessage: {message}"
        text = provider.answer([{"role": "user", "content": prompt}], model=model, system=SYSTEM)
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
        return {"route": data.get("route"), "tool": data.get("tool"), "raw": (text or "")[:80]}
    return classify
