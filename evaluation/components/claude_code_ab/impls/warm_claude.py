"""ALTERNATIVE (live arm): Claude Code given the offline IDF hint (or the case's deliberately wrong hint). Needs the claude CLI
and spends tokens; waived in waivers.csv S10 (claude-code-ab live arms)."""
from claude_code_ab import live_support as _live


def build(meter):
    return _live.runner(meter, warm=True)
