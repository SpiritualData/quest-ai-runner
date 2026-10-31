"""ALTERNATIVE (live arm): Claude Code alone (haiku, headless) in a copy of the repo, no hint. Needs the claude CLI and spends
tokens; waived in waivers.csv S10 (claude-code-ab live arms)."""
PAID = True
from claude_code_ab import live_support


def build(meter):
    return live_support.runner(meter, warm=False)
