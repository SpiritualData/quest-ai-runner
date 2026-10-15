"""ALTERNATIVE (live arm): Claude Code alone (haiku, headless) in a copy of the repo, no hint. Needs the claude CLI and spends
tokens; waived in waivers.csv S10 (claude-code-ab live arms)."""
from claude_code_ab import live_support as _live


def build(meter):
    return _live.runner(meter, warm=False)
