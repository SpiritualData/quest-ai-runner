"""The in-situ arm's engine: a case scenario sent through QAR's real deep-run prompt, with tools disabled.

QAR's deep worker is ``claude -p`` started with a prompt that ``compose_goal_prompt`` builds: the standing
instructions (``prompts/evolvable_structure.md``, a required part), then the TASK, the CONTEXT USAGE and
HEADLESS RUN CONTRACT directives, and the instruction to summarise what was changed. This module builds
exactly that prompt with the real function, and runs it through the same headless print path as the deep
worker, with two differences that make it safe and cheap:

* ``--tools ""`` removes every tool, so nothing can be read, edited or run; the model can only describe the
  actions it would take. A final line in the prompt says so (the same line in both arms) so the model
  describes its edits instead of asking for access.
* the cheapest tier (``QAR_EVAL_MODEL``, default haiku) and a neutral working directory.

Not reproduced: the lane's org preamble (context cards, persona), the goal loop, and real files for the
model to change. The reply is therefore the deep worker's plan and stated actions, not a diff. ``plain`` is
the same path with the standing-instructions layer emptied.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import common  # noqa: F401  (sets sys.path for the repo)

SANDBOX_NOTE = ("EVALUATION SANDBOX: no tools, files or shell are available in this run. State concretely the "
                "exact edits, files and actions you would take, and show any code or text you would write.")


def compose(case, compose_goal_prompt, standing):
    """The real deep-run prompt for this case, built by QAR's ``compose_goal_prompt`` (passed in by the arm);
    ``standing`` False empties the standing-instructions layer."""
    from quest_ai_runner.core import standing_instructions
    saved = standing_instructions.load_standing_instructions
    if not standing:
        standing_instructions.load_standing_instructions = lambda: ""
    try:
        prompt = compose_goal_prompt("", case["scenario"], preamble="")
    finally:
        standing_instructions.load_standing_instructions = saved
    has_layer = "Evolvable Structure that Optimizes Intent" in prompt
    if has_layer != standing:
        raise RuntimeError(f"standing instructions {'missing from' if standing else 'present in'} the deep prompt")
    return prompt + "\n\n" + SANDBOX_NOTE


def build_runner(meter, model):
    """``run(prompt) -> reply text`` through the headless CLI with no tools, counted by ``meter``."""
    from quest_ai_runner.adapters.claude_cli_provider import cli_model

    binary = shutil.which("claude") or str(Path("~/.local/bin/claude").expanduser())
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")}
    workdir = Path(tempfile.gettempdir()) / "qar-cli-completion"  # empty: no CLAUDE.md above it
    workdir.mkdir(parents=True, exist_ok=True)

    def run(prompt):
        meter.record()
        cmd = [binary, "-p", "--output-format", "json", "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--model", cli_model(model) or model]
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"), env=env, cwd=str(workdir),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        envelope = json.loads(proc.stdout.decode("utf-8", "replace"))
        if envelope.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(envelope.get('result'))[:200]}")
        usage = envelope.get("usage") or {}
        meter.tokens_in += sum(int(usage.get(k) or 0) for k in
                               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        meter.tokens_out += int(usage.get("output_tokens") or 0)
        meter.cost_usd += float(envelope.get("total_cost_usd") or 0.0)
        return envelope.get("result") or ""
    return run
