"""Slash-command vocabulary, the help text and the startup banner.

Split out of ``interactive_session.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations




# ── Slash commands ────────────────────────────────────────────────────────────
#
# The canonical command list. The UI owns input handling and completion; this is
# the vocabulary it completes against and dispatches on.

_SLASH_COMMANDS = [
    "/help", "/clear", "/reps", "/rep ", "/file ",
    "/whoami", "/status", "/tasks",
    "/quit", "/q",
    "/models", "/model", "/model ", "/depth", "/depth ",
    "/system", "/replan", "/pause", "/resume",
    "/save ", "/save", "/load ", "/sessions",
    "/quest", "/quest ",
]


# /models — interactive model tier selection menu
# /model [haiku|sonnet|opus|fable] — set or show current model tier (no arg → menu)
# /depth [light|standard|deep] — alias for /model (light=haiku, standard=sonnet, deep=opus)
# /system [text] — set or show a custom system-prompt prepended to the persona
# /replan — prime the next turn to force a fresh re-planning pass at opus tier
# /save [name] — save session to ~/.quest-ai-runner/sessions/<name>.json
# /load <name> — restore a saved session
# /sessions — list saved sessions


# ── Session ───────────────────────────────────────────────────────────────────

_HELP = """\
Commands:

  ● Session & Configuration
    /whoami              Show AI identity and session state
    /status              Show token usage and speed metrics
    /tasks               Show recently completed tasks
    /reps                Choose an AI representative (skill files)
    /rep <name>          Set representative name directly
    /file <path>         Load a persona file

  ● Model & Behavior
    /models              Choose a model from a list
    /model [tier]        Choose a model, or set one: haiku, sonnet, opus, fable
    /depth [level]       Alias for /model: light=haiku, standard=sonnet, deep=opus
    /system [text]       Show or set a custom system prompt prepended to persona
    /replan              Prime next turn for a fresh re-planning pass (uses opus)
    /pause               Pause the live deep worker, keeping its work (stream_json runner)
    /resume              Continue a paused or stopped deep run in the same session

  ● Quest
    /quest               Select a quest for this conversation
    /quest <name>        Select a quest by name or id
    /quest auto          No selected quest; match each message to one (default)
    /quest none          No quest at all, not even matched (remembered)

  ● Sessions
    /save [name]         Save this session (transcript + config) to disk
    /load <name>         Restore a saved session
    /sessions            List saved sessions

  ● Conversation
    /clear               Start fresh: the AI forgets this conversation so far
    /help                Show this help

  ● Exit
    /quit, /q            Exit the session

Keys:

  /              Open the command menu (arrows move, Tab completes, Enter runs, Esc closes)
  Esc            Cancel the current turn, or close an open menu
  (typing)       While the work runs, a note you send is read at its next step (within seconds for a live worker)
  Ctrl+C         Copy the selected text; with nothing selected, exit
  Alt+Y          Copy the last AI reply
  Ctrl+L         Clear the screen
  Shift+Enter    New line (also Ctrl+J, Alt+Enter, or a backslash before Enter)
  PageUp/Down    Scroll the transcript
  Tab            Next job's detail (completes a command while the menu is open)
  Alt+D / Alt+C  Show the job's detail and metric results / show the sources used

Resume a conversation later (run outside this session):

  quest-ai-runner chat --resume          Pick a saved conversation from a list
  quest-ai-runner chat --continue        Continue the most recent one (same as --resume last)

Companion CLI commands (run outside this session, e.g. in another terminal):

  quest-ai-runner search-context "<query>"   See which context cards a query would surface
  quest-ai-runner bootstrap --dry-run        Estimate cost/time to index this corpus, before running it
"""


_BANNER = """\
{B}{C}Quest AI Runner{R}  Grounded AI that acts like a colleague

  What makes this different:
  ● finds just the right context efficiently; no "look at this file" needed
  ● routes to the right model automatically (haiku → sonnet → opus)
  ● optimal token usage; can run forever without context bloat
  ● named AI reps learn how to act like their associated human

  {D}/ for commands  ·  Esc to cancel  ·  Ctrl+C to exit  ·  /help for everything{R}
"""
