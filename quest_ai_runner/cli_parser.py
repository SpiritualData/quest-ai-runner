"""The argparse tree for every `quest-ai-runner` subcommand.

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import argparse

from .cli_chat import RESUME_PICK


def _build_parser() -> argparse.ArgumentParser:
    """The full argparse tree: `chat`, `quest`, `send`, ... `poll`, `channel`, plus the legacy flat poll flags."""
    parser = argparse.ArgumentParser(prog="quest-ai-runner",
                                     description="Poll Quest for due AI tasks and execute them.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="increase verbosity: -v for info, -vv for debug")
    sub = parser.add_subparsers(dest="command")

    # --- chat subcommand: interactive attended session ------------------------
    chat_p = sub.add_parser("chat", help="start an interactive session with the brain")
    chat_p.add_argument("rep_positional", nargs="?", default=None, metavar="REP",
                        help="shorthand for --rep NAME (e.g. `qar wadona`); if a "
                             "<corpus_root>/<name>/CLAUDE.md file exists it is also loaded as "
                             "the persona, same as --persona-file")
    chat_p.add_argument("--rep", default=None, metavar="NAME",
                        help="AI representative display name shown in the session "
                             "(default: QAR_REP_NAME env var, else 'Assistant')")
    chat_p.add_argument("--persona-file", default=None, metavar="PATH",
                        help="path to a persona/skill file injected into every turn "
                             "(default: QAR_REP_PERSONA_FILE env var)")
    chat_p.add_argument("--goal-id", default=None, metavar="QUEST_ID",
                        help="start with this quest selected for the conversation (same as "
                             "/quest <id> inside the chat)")
    chat_p.add_argument("--config", default=None, metavar="PATH",
                        help="TOML config file (see docs/writing-a-consumer.md); "
                             "QAR_CONFIG_FILE also works. Environment variables always win over "
                             "a value the file also sets.")
    chat_p.add_argument("--check", action="store_true",
                        help="validate chat prerequisites (model provider, context store) and exit, "
                             "without opening the terminal UI")
    chat_p.add_argument("--resume", "-r", nargs="?", const=RESUME_PICK, default=None,
                        metavar="ID",
                        help="continue a saved chat conversation. With no ID, pick one from a "
                             "searchable list (like Claude Code); `--resume last` takes the most "
                             "recent one for this corpus; otherwise the conversation whose id "
                             "starts with ID. Its turns are restored as context and new turns "
                             "append to the same conversation")
    chat_p.add_argument("--continue", "-c", dest="resume", action="store_const", const="last",
                        help="continue the most recent conversation for this corpus "
                             "(same as --resume last)")
    chat_p.add_argument("--list-conversations", action="store_true",
                        help="list saved chat conversations for this corpus (newest first) and exit")

    # --- quest subcommand: call the Quest API from the shell -------------------
    quest_p = sub.add_parser(
        "quest", help="call a QuestClient method and print the JSON result",
        description="Call any QuestClient method by name against the configured Quest account "
                    "and print the result as JSON. Read-only methods (whoami/get_*/list_*/...) "
                    "run as-is; anything that writes needs --write.")
    quest_p.add_argument("method", nargs="?", default=None,
                         help="QuestClient method name, e.g. whoami, get_task, list_my_quests")
    quest_p.add_argument("args", nargs="*", default=[],
                         help="positional arguments for the method (strings)")
    quest_p.add_argument("--kw", action="append", default=[], metavar="NAME=VALUE",
                         help="keyword argument; VALUE is parsed as JSON when it looks like JSON, "
                              "otherwise passed as a string (repeatable)")
    quest_p.add_argument("--write", action="store_true",
                         help="allow a method that modifies Quest (required for those)")
    quest_p.add_argument("--list", action="store_true", dest="list_methods",
                         help="list the callable methods and exit")
    quest_p.add_argument("--describe", default=None, metavar="METHOD",
                         help="show how to call one method (what it does, when to use it, its "
                              "arguments) and exit")
    quest_p.add_argument("--config", default=None, metavar="PATH",
                         help="TOML config file (QAR_CONFIG_FILE also works)")
    quest_p.add_argument("--compact", action="store_true",
                         help="one-line JSON instead of indented")

    # --- send subcommand: enqueue a new AI task -------------------------------
    send_p = sub.add_parser("send", help="enqueue a new AI task and print its id")
    send_p.add_argument("text", help="the task instruction")
    send_p.add_argument("--team-id", default=None,
                        help="route to this team (default: QUEST_TEAM_ID env var)")
    send_p.add_argument("--goal-id", default=None, help="attach to this goal id")
    send_p.add_argument("--at", dest="scheduled_at", default=None,
                        help="ISO-8601 UTC datetime to schedule (omit = run at next poll)")
    send_p.add_argument("--card", action="append", dest="card_ids", metavar="CARD_ID",
                        help="Attach a context card (topic) to the task; repeatable.")
    send_p.add_argument("--no-auto-cards", action="store_true",
                        help="Do not auto-select context cards from the task text when no "
                             "--card is given.")

    # --- reconcile-plan subcommand: roll a quest's day/week goals forward, turn open asks into goals -----
    rec_p = sub.add_parser("reconcile-plan",
                           help="roll unfinished day/week goals forward and add open asks as goals for their assignee")
    rec_p.add_argument("quest_id", help="the quest whose plan to reconcile")
    rec_p.add_argument("--today", default=None, help="the date to treat as today (YYYY-MM-DD); default: today")
    rec_p.add_argument("--write", action="store_true", help="apply the changes (default: dry run, print only)")

    # --- create-goal subcommand: create a real, typed Goal (not an AI task -- see `send`) -----
    goal_p = sub.add_parser("create-goal",
                            help="create a real Quest goal (period-scoped, on a quest's plan) and print its id")
    goal_p.add_argument("title", help="the goal's title")
    goal_p.add_argument("--quest-id", default=None,
                        help="attach to this quest, so it shows on the quest's plan for every "
                             "team member (recommended); omit for a standalone goal on the "
                             "caller's own account")
    goal_p.add_argument("--period", default=None,
                        help="YYYY-MM-DD (day) / YYYY_W## (week) / YYYY_MM (month) / YYYY_Q# "
                             "(quarter) / YYYY (year); default: today")
    goal_p.add_argument("--description", default=None, help="longer freeform description")
    goal_p.add_argument("--criteria", default=None, help="completion criteria")
    goal_p.add_argument("--goal-type", default=None, help="e.g. day_goal, week_goal")
    goal_p.add_argument("--parent-goal-id", default=None, help="parent goal id, for a sub-goal")
    goal_p.add_argument("--outcome-id", default=None,
                        help="the quest measurable outcome this goal moves (needs --quest-id)")
    goal_p.add_argument("--ai-help", action="store_true",
                        help="mark this goal AI-assisted (Quest Autopilot may act on it)")
    goal_p.add_argument("--assigned-to-user-id", default=None,
                        help="assign the goal to this Quest member's user_id at creation "
                             "(requires --quest-id; refused if they are not a member)")

    # --- bootstrap subcommand: build/refresh the context card store ----------
    boot_p = sub.add_parser("bootstrap", help="build or refresh the context card store for the corpus")
    boot_p.add_argument("--corpus", default=None, metavar="PATH",
                        help="corpus root (default: QAR_CORPUS_ROOT env var)")
    boot_p.add_argument("--cards-dir", default=None, metavar="PATH",
                        help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    boot_p.add_argument("--force", action="store_true",
                        help="delete all existing cards and bootstrap from scratch (forces re-index when algorithm changes)")
    boot_p.add_argument("--dry-run", action="store_true",
                        help="estimate tokens, cost, and time without running bootstrap")
    boot_p.add_argument("--yes", "-y", action="store_true",
                        help="skip the first-run confirmation for a large corpus")
    boot_p.add_argument("--prune-only", action="store_true",
                        help="remove cards the corpus no longer indexes (deleted files, files now "
                             "inside a skipped directory, and cards that only restate their own "
                             "path) and stop — no model calls, nothing re-analysed")

    # --- paste-context subcommand: save context to a card -----------------------
    paste_p = sub.add_parser("paste-context", help="save context to a context card (from args or stdin)")
    paste_p.add_argument("context", nargs="?", default=None,
                         help="context text (if not provided, reads from stdin)")
    paste_p.add_argument("--card-id", default=None,
                         help="card id or key to save under (default: auto-generate from content)")
    paste_p.add_argument("--cards-dir", default=None, metavar="PATH",
                         help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    paste_p.add_argument("--corpus", default=None, metavar="PATH",
                         help="corpus root (default: QAR_CORPUS_ROOT env var)")
    paste_p.add_argument("--goal-id", default=None,
                         help="optional: quest goal id for metadata")

    # --- search-context subcommand: show what context cards a query would surface --
    sc_p = sub.add_parser("search-context", help="show which context cards a query selects")
    sc_p.add_argument("query", help="the input text to search context for")
    sc_p.add_argument("--corpus", default=None, metavar="PATH",
                      help="corpus root (default: QAR_CORPUS_ROOT env var or cwd)")
    sc_p.add_argument("--cards-dir", default=None, metavar="PATH",
                      help="cards directory (default: <corpus>/.quest-context)")
    sc_p.add_argument("--no-llm", action="store_true",
                      help="skip LLM relevance filter, show raw IDF results only")

    # --- context subcommand: what is the context for this quest right now --------
    ctx_p = sub.add_parser(
        "context",
        help="show a quest's current context updates (a read: safe, repeatable, no side effects). "
             "Card-store maintenance lives under the same word: `context move-root`, "
             "`context migrate-imports`, `context merge-into`, `context store-info` (each a dry run unless --apply).")
    ctx_p.add_argument("quest_id", help="the quest to read (e.g. quest_1625d9f47a06)")
    ctx_p.add_argument("--days", type=float, default=None, metavar="N",
                       help="look back N days instead of from the stored 'last looked' stamp")
    ctx_p.add_argument("--since", default=None, metavar="WHEN",
                       help="look back from this ISO-8601 timestamp (e.g. 2026-09-01 or "
                            "2026-09-01T06:00:00Z) instead of the stored stamp")
    ctx_p.add_argument("--source", action="append", default=None, metavar="NAME",
                       help="only this source, repeatable (default: every source the quest "
                            "watches). --sources lists the vocabulary.")
    ctx_p.add_argument("--sources", action="store_true",
                       help="list the source names a quest's context_sources can use, then exit")
    ctx_p.add_argument("--tracked", action="store_true",
                       help="list what has been asked for on this quest and where each one got "
                            "to (the feedback ledger), then exit")
    ctx_p.add_argument("--block", action="store_true",
                       help="print ONLY the prompt block a run would be handed, nothing else")
    ctx_p.add_argument("--json", action="store_true", dest="as_json",
                       help="print the bundle as JSON (updates, per-source reports, the block)")
    ctx_p.add_argument("--config", default=None, metavar="PATH",
                       help="TOML config file (see docs/writing-a-consumer.md); QAR_CONFIG_FILE "
                            "also works. Environment variables always win over the file.")

    # --- cards subcommand: look at a context card store ----------------------------
    cards_p = sub.add_parser(
        "cards", help="look at a context card store: `cards report` says what fills it, what its card "
                      "limit (QAR_MAX_CARDS) has done, and how to change the limit; `cards tidy` moves "
                      "out the cards that are provably copies (a dry run unless --apply)")
    cards_sub = cards_p.add_subparsers(dest="cards_op", required=True)
    report_p = cards_sub.add_parser("report", help="print the card limit report for a store (read only)")
    report_p.add_argument("--cards-dir", default=None, metavar="PATH",
                          help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    report_p.add_argument("--corpus", default=None, metavar="PATH",
                          help="corpus root (default: QAR_CORPUS_ROOT env var or cwd)")
    report_p.add_argument("--write", action="store_true",
                          help="also rewrite card_limit_report.md inside the store with this report")
    report_p.add_argument("--config", default=None, metavar="PATH",
                          help="the lane's TOML config file (QAR_CONFIG_FILE also works): its environment says "
                               "where the lane's cards live (a local folder, the Quest backend or Qdrant)")
    tidy_p = cards_sub.add_parser(
        "tidy", help="tidy the lane's card store. A local folder: move out the cards that are provably copies of "
                     "other cards (to _quarantine/, never deleted). The Quest backend or Qdrant: the full tidy "
                     "(copies, cards about excluded folders, duplicates, thin cards), after a complete local "
                     "export that --undo restores from. A card with notes, usage, quest tags or a hand-written "
                     "origin is never removed. A dry run unless --apply")
    tidy_p.add_argument("--config", default=None, metavar="PATH",
                        help="the lane's TOML config file (QAR_CONFIG_FILE also works): its environment says "
                             "where the lane's cards live")
    tidy_p.add_argument("--cards-dir", default=None, metavar="PATH",
                        help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    tidy_p.add_argument("--corpus", default=None, metavar="PATH",
                        help="corpus root (default: QAR_CORPUS_ROOT env var or cwd)")
    tidy_p.add_argument("--apply", action="store_true",
                        help="move the cards; without this nothing is touched")
    tidy_p.add_argument("--undo", default=None, metavar="MANIFEST",
                        help="take back the moves a tidy manifest records (cards go back into the index and what "
                             "was joined onto the cards that stood for them is taken off again); the manifests are "
                             "the *-tidy-manifest.jsonl files in <cards dir>/_quarantine, and for a tidy through "
                             "the repository the *-store-tidy-manifest.jsonl files in <cards dir>/tidy-state"),

    # --- sync-quest-folder subcommand: pull/push a quest <-> a local folder --
    sync_p = sub.add_parser("sync-quest-folder",
                            help="sync a Quest quest's state/notes with a local folder's QUEST_SYNC.md")
    sync_p.add_argument("quest_id", help="the quest id to sync (e.g. quest_1625d9f47a06)")
    sync_p.add_argument("folder", help="local folder holding (or to hold) QUEST_SYNC.md")
    sync_p.add_argument("--direction", choices=["pull", "push", "both"], default="pull",
                        help="pull: Quest -> folder (default); push: folder -> Quest; "
                             "both: pull then push")

    # --- poll subcommand (and legacy flat flags, kept for back-compat) --------
    poll_p = sub.add_parser("poll", help="poll Quest for due tasks and run them")
    poll_p.add_argument("--once", action="store_true", help="one scan then exit (cron mode)")
    poll_p.add_argument("--check", action="store_true", help="validate config + key, then exit")
    poll_p.add_argument("--config", default=None, metavar="PATH",
                        help="TOML config file (see docs/writing-a-consumer.md); "
                             "QAR_CONFIG_FILE also works. Environment variables always win over "
                             "a value the file also sets.")

    # --- channel subcommand: hold a live, two-way conversation over a messaging channel -------
    channel_p = sub.add_parser(
        "channel", help="drive a live ChannelTransport (e.g. OpenClaw) as a real-time chat lane")
    channel_p.add_argument("--once", action="store_true",
                           help="one receive+dispatch pass then exit (mainly for testing)")
    channel_p.add_argument("--check", action="store_true",
                           help="validate the channel config (provider, allowed senders), then exit")
    channel_p.add_argument("--config", default=None, metavar="PATH",
                           help="TOML config file (see docs/writing-a-consumer.md); "
                                "QAR_CONFIG_FILE also works. Environment variables always win over "
                                "a value the file also sets.")

    # --- serve-files: a read-only file server so a remote brain can read this machine's files ---
    files_p = sub.add_parser(
        "serve-files", help="serve one directory tree read-only over HTTP (needs QAR_FILES_TOKEN)")
    files_p.add_argument("--root", default=None, metavar="DIR",
                         help="the directory tree to serve (QAR_FILES_ROOT also works)")
    files_p.add_argument("--host", default=None, metavar="ADDR",
                         help="address to bind (default 127.0.0.1; QAR_FILES_HOST also works)")
    files_p.add_argument("--port", default=None, type=int, metavar="PORT",
                         help="port to listen on (default 8765; QAR_FILES_PORT also works)")

    # Legacy: flags directly on the root command (no subcommand given) stay working; also
    # documented on `poll` above. Kept visible (not argparse.SUPPRESS) so `-h` shows them.
    parser.add_argument("--once", action="store_true",
                        help="poll mode: one scan then exit (cron mode); same as 'poll --once'")
    parser.add_argument("--check", action="store_true",
                        help="poll mode: validate config + key, then exit; same as 'poll --check'")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="poll mode: TOML config file; same as 'poll --config'. "
                             "QAR_CONFIG_FILE also works.")
    return parser
