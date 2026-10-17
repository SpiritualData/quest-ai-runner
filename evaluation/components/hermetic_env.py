"""One shared guard for component evaluations that drive live runner code: no shell or lane setting reaches
the run. With Quest credentials in the environment the context assembler would wire the live card backend,
and a QAR_* variable from a deployed lane would change what a case measures. This is the same scrub the
repo's own test suite applies before every test."""
import contextlib
import os

WEB_KEYS = ("QAR_WEB_SEARCH_BACKEND", "SERPER_API_KEY", "BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY",
            "WEB_SEARCH_API_KEY", "SEARXNG_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY")
QUEST_KEYS = ("QUEST_BASE_URL", "QUEST_API_URL", "QUEST_API_KEY", "QUEST_TEAM_ID", "QUEST_TEAM_IDS", "QUEST_ORG_ID",
              "CLAUDE_CONFIG_DIR")


@contextlib.contextmanager
def hermetic_env(extra=None):
    """Scrub QAR_*, web-search and Quest variables for the block, set ``extra`` on top, restore on exit."""
    saved = dict(os.environ)
    for name in [n for n in os.environ if n.startswith("QAR_") or n in WEB_KEYS or n in QUEST_KEYS]:
        del os.environ[name]
    os.environ.update(QAR_STANDARD_TOOLS="0", QAR_CLAUDE_PATH="qar-eval-no-such-claude", QAR_CLAUDE_LIVE_MODELS="0")
    os.environ.update(extra or {})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
