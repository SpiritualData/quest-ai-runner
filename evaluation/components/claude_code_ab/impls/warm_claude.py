"""ALTERNATIVE (live arm): Claude Code given the offline IDF hint (or the case's deliberately wrong hint). Needs the claude CLI
and spends tokens; waived in waivers.csv S10 (claude-code-ab live arms)."""
PAID = True
from claude_code_ab import live_support


def build(meter):
    return live_support.runner(meter, warm=True)
