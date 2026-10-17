"""LIVE arm: one model call per case with QAR's shipped standing ESOI instructions
(``quest_ai_runner/prompts/evolvable_structure.md``) as the system prompt. Costs real money; run on a
subset (``--only``) and with ``--max-calls``. Compare with ``plain`` on the same cases."""
import os
from pathlib import Path

import common  # noqa: F401  (sets sys.path for the repo)

PROMPT = Path(common.REPO) / "quest_ai_runner" / "prompts" / "evolvable_structure.md"


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    provider = meter.wrap(ClaudeCliProvider())
    model = os.environ.get("QAR_EVAL_MODEL", "haiku")
    system = PROMPT.read_text()

    def respond(case):
        return provider.answer([{"role": "user", "content": case["scenario"]}], model=model, system=system) or ""
    return respond
