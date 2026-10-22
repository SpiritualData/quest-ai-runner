# The terminal chat: `quest-ai-runner chat`

`quest-ai-runner chat` opens an interactive chat with the same brain that runs your Quest AI
tasks. It grounds each answer in your corpus and your quests, streams its thinking as it works,
and hands bigger jobs to a deep worker (Claude Code) that it tracks live on screen. Conversations
are saved as you go, so you can close the terminal and pick up where you left off, the way
`claude --resume` works in Claude Code.

This page covers starting a chat, resuming one, the `/` command menu, choosing a quest for the
conversation, and the keyboard.

- [Start a chat](#start-a-chat)
- [Resume a conversation](#resume-a-conversation)
- [Slash commands](#slash-commands)
- [Choosing a quest for the conversation](#choosing-a-quest-for-the-conversation)
- [Keys](#keys)
- [Environment variables](#environment-variables)

## Start a chat

The chat needs a model provider (an API key, or a logged-in `claude` CLI) and, to be grounded in
your own material, a corpus root:

```bash
export QAR_CORPUS_ROOT=/path/to/your/docs     # what the chat reads and grounds answers in
quest-ai-runner chat
```

Many people alias it: `alias qar="quest-ai-runner chat"`, then `qar`, `qar --resume`, and so on.

| Command | What it does |
|---|---|
| `quest-ai-runner chat` | Start a new conversation. |
| `quest-ai-runner chat <name>` | Start as the AI representative `<name>`. If `<corpus_root>/<name>/CLAUDE.md` exists it is loaded as the persona. |
| `--rep NAME` | The representative's display name (default: `QAR_REP_NAME`, else `Assistant`). |
| `--persona-file PATH` | A persona or skill file added to every turn (default: `QAR_REP_PERSONA_FILE`). |
| `--goal-id QUEST_ID` | Start with this quest selected, the same as `/quest <id>` inside the chat. |
| `--config PATH` | A TOML config file (see [Writing a consumer](writing-a-consumer.md)); environment variables still win. |
| `--check` | Check that the chat can start (model provider, context store) and exit, without opening it. |

Quest credentials (`QUEST_BASE_URL`, `QUEST_API_KEY`, and `QUEST_TEAM_ID` or `QUEST_TEAM_IDS`) are
optional. Without them the chat still works; with them it can also list and select every quest
your account can reach (see [Choosing a quest](#choosing-a-quest-for-the-conversation)).

## Resume a conversation

Every conversation is saved after each exchange. When you exit (Ctrl+C or `/quit`), the chat
prints the exact command that reopens it:

```text
Resume this session with:
  quest-ai-runner chat --resume 0123abcd0123abcd0123abcd0123abcd
```

| Command | What it does |
|---|---|
| `quest-ai-runner chat --resume` (or `-r`) | Opens a searchable list of saved conversations, newest first. Arrows move, Enter resumes, typing filters by anything said in the conversation, Esc cancels. |
| `quest-ai-runner chat --continue` (or `-c`) | Continues the most recent conversation for this corpus. Same as `--resume last`. |
| `quest-ai-runner chat --resume <id>` | Continues that conversation. Any unique start of the id works. |
| `quest-ai-runner chat --list-conversations` | Prints the saved conversations (id, age, turns, first message) and exits. |

What resuming does:

- **The earlier turns become the conversation's history again,** so "what did I just ask" and "do
  that one" work as if the chat never closed.
- **New turns append to the same conversation,** so one conversation stays one record.
- **The last 10 turns are shown at once,** before the rest of the session finishes loading, under one
  plain line (`Picking up your last conversation (2 turns).`; the conversation's id is added with `-v`).
  A longer conversation notes how many earlier turns are not shown. The session line and one `Ready.`
  come when the session has finished loading, the same two lines as a fresh start.
- **The quest selected in that conversation is selected again,** unless you pass `--goal-id`.

"For this corpus" means `--resume` (list) and `--continue` skip conversations recorded under a
different `QAR_CORPUS_ROOT`. An explicit id finds a conversation from any corpus.

Where conversations live: one JSON file per conversation in `QAR_CHAT_HISTORY_DIR` (default
`~/.quest-ai-runner/conversations/qar_chat_<id>.json`), holding the messages plus the corpus root,
selected quest, representative name and last-update time. The chat also searches these files for
earlier conversations related to what you are asking now.

Warnings and errors do not fill the transcript. The full record (time, level, logger, traceback)
always goes to a log file, `logs/chat.log` beside the saved conversations (`QAR_CHAT_LOG_FILE`
overrides it; it is rotated and capped at about 1 MB plus two older files). The transcript shows a
short dim `⚠` line for each of the first three distinct problems raised by this package. Once they stop
arriving, one closing line counts the rest and says where the details are
(`4 more notices. Details: ~/.quest-ai-runner/logs/chat.log`), and no later warning is shown. A
library's own warnings are never shown there. `chat -v` writes every record in full to the transcript
instead.

While the AI works, the status line counts the time once a wait passes 3 seconds, ahead of the words
(`45s  Getting ready to start the work…`), so a long silent step never looks frozen and a narrow
terminal never cuts the clock; a long status is shortened with `…` to fit. Times are written one way
everywhere (`45s`, `3m32s`), from the start of the turn. The count stops when the live work panel
takes over (it carries the same clock) or the turn ends.

The live panel for work in progress is as tall as its text, up to 12 rows. The transcript keeps its full
width while it is open.

`/save` and `/load` are different: they save and restore a named snapshot of the session's
settings and transcript. Resuming needs neither; every conversation is already saved.

## Slash commands

Type `/` in the prompt and a menu opens above it, listing every command with what it does. It
narrows as you type. Arrows move, Tab completes the highlighted command, Enter runs it, Esc closes
the menu. A command that takes an argument (like `/quest <name>`) completes to the command and
keeps the menu open for the argument.

Commands that offer a choice (`/quest`, `/model`, `/models`, `/reps`) open a chooser in the same
place: arrows move, Enter selects, typing narrows the list, Esc cancels. Typing the option's
number and pressing Enter also works.

A line that starts with `/` is always a command for the chat itself, never a message to the AI.
It runs straight away, even while a turn is in progress. One typed while the chat is still
starting up runs as soon as it is ready.

| Command | What it does |
|---|---|
| **Quest** | |
| `/quest` | Select a quest for this conversation. |
| `/quest <name>` | Select a quest by name or id. |
| `/quest auto` | No selected quest; each message is matched to the quest it is about (the default). |
| `/quest none` | No quest at all, not even matched. Remembered for next time. |
| **Model and behavior** | |
| `/model [tier]` | Choose a model, or set one directly: `haiku`, `sonnet`, `opus`, `fable`, or `auto`. |
| `/models` | Choose a model from a list. |
| `/depth [level]` | Shorthand for `/model`: `light` (haiku), `standard` (sonnet), `deep` (opus). |
| `/system [text]` | Show or set a custom system prompt added before the persona. |
| `/replan` | Make the next turn re-plan from scratch with the strongest model. |
| **Session and configuration** | |
| `/whoami` | Show the AI's identity and the session's state. |
| `/status` | Show token usage and speed. |
| `/tasks` | Show recently completed tasks. |
| `/reps` | Choose an AI representative (from the skill files). |
| `/rep <name>` | Set the representative's name. |
| `/file <path>` | Load a persona file. |
| **Sessions** | |
| `/save [name]` | Save a named snapshot of this session. |
| `/load <name>` | Restore a snapshot. |
| `/sessions` | List saved snapshots. |
| **Conversation** | |
| `/clear` | Start fresh: the AI forgets this conversation so far (the screen is kept; Ctrl+L clears it). |
| `/help` | Show all commands and keys. |
| `/quit`, `/q` | Exit (and print the command to resume). |

`/quests` and `/goal` still work and open `/quest`; they were the older way to attach a chat to a
goal.

## Choosing a quest for the conversation

A quest can shape a conversation in three ways, and `/quest` switches between them.

**Selected.** `/quest` lists every quest the chat can reach: the quest folders synced to this
machine first (each shown with its folder), then the quests from Quest itself (shown as not synced
to a local folder). Pick one and every message in the conversation is about that quest, exactly as
when you choose a quest in the Quest AI chat app: the turn is bound to it, its context comes first,
and other quests' context is kept out. The chat shows `Selected quest: ...` before each turn.

**Matched automatically** (the default). With no quest selected, each message is matched to the
quest it is clearly about, by the quest's name, its id, or several distinctive words from its
current state ("did we fix the pricing page deep link" can find a quest without naming it). A
match only puts that quest's context card first; it does not bind the turn or hide anything else.
A message about no quest in particular matches nothing, and a follow-up like "what's next for it?"
is matched together with the message before it. The chat shows `Matched quest: ...` when it
happens.

**None.** `/quest none` stops quests from being added at all, for people whose conversations are
mostly not about a quest. The choice is remembered (in `qar_state.json`). A deployment can make it
the default with `QAR_QUEST_AUTO_MATCH=0`; a person's own choice still wins.

Where quests come from:

- **Synced quest folders.** Any folder under the corpus root (or the directory the chat was
  started in, if no corpus root is set) whose `QUEST_SYNC.md` declares a `quest_id`, plus the
  folders in `quest_folder_map`. See [GOALS.md and quest folders](quest-folder-goals.md). Each one
  becomes a context card holding the folder's full path, its files, the quest's current state and
  its standing next steps, refreshed whenever `QUEST_SYNC.md` changes.
- **Quest itself.** With Quest credentials set, the chat also lists every quest of each team in
  `QUEST_TEAM_ID` / `QUEST_TEAM_IDS` plus the account's own quests, fetched in the background when
  the chat starts.

If the list is empty, the chooser says why: where it looked for quest folders, and whether Quest
credentials are set.

## Steering a running deep worker

With the `stream_json` deep runner selected (`ESOI_SELECT_QUEST_AI_RUNNER_RUN_DEEP_WORK=stream_json`,
see [stream-json-runner.md](stream-json-runner.md)), a message typed while the worker runs reaches it
within a second, `/pause` pauses it and `/resume` continues the same session (straight to a deep
run of the paused goal, never through the planner). With the default
one-shot runner a message typed mid-run is queued and acted on when the turn ends.

## What the screen says about context and results

- **Context.** After a request the AI says what it found, in one sentence: `Found 6 sources: <title>,
  <title> and 4 more.` ("Sources" is the one word for the notes and files it looked at; the same count
  appears in the closing line and in `How I got this`, and Alt+C lists them under `Sources used`.) Files a
  job opened are counted in its own summary line (`3 files read · 1 edit · 3 commands`), never as sources.
  Nothing in the data says which sources the plan went on to use, so the two named are the retrieval's best
  matches, not a claim about use.
- **Metrics.** The metric before and after is written from data, before the result so a long answer does not
  bury it: `<name>: 0.38 -> 0.91 (measured automatically)`, `(reported by the AI)` for the worker's own
  record, `Nothing to measure: <reason>` for a declared not-applicable. Numbers have at most 3 decimals
  and no unit is invented (the data has none). When the library flags the measured and reported numbers as
  different, the AI's follow in the same line, neutrally (`The AI reported 0.4 -> 0.9.`). A long name wraps;
  it is never cut. The first four are written; for the rest the line says `8 more metric results. Alt+D shows
  them all.` and Alt+D lists every one above the job's actions (with no job to open, none are folded away).
- **Notes typed while it works.** Each appears as your own line with a small tag (`Also check the staging
  sheet.  (waiting)`); the count of notes waiting is in the status strip only. When the AI picks them up
  one plain line says so (`Working your 2 notes in now.`).
- **Understood as.** The AI's reading of your request is italic and indented, never the bold cyan of your
  own lines.

## How a turn ends

Each turn ends with one quiet closing line that says how it ended, how long it took, the model it
ran on (the short name, such as `sonnet`; the full id with `-v`) and the keys that open more:

| Line | Meaning |
|---|---|
| `✓ Done in 3m32s` | Every goal was met. |
| `~ Stopped before finishing · 1m29s` | The work ran and did not finish. |
| `✗ Nothing ran` | The work could not start. A short note above says why, in plain words, and nothing is retried. |
| `Paused at Claude Code's usage limit. It resets at 15:40 UTC.` | The subscription's limit was reached. The time is only ever one Claude Code reported; without one the line says it has not said when. |
| `Waiting for your answer` | The AI asked a question. The question is the last thing on screen, from its speaker. |

A plan that was written but never ran gets one automatic second try, with the reason on screen.
There is never a third pass, and none after a start failure, a usage limit, a question or a pause.
A failed model call is explained in a sentence, and your message is put back in the prompt box so
Enter sends it again.

The transcript follows the newest output. Scrolling up (PageUp, the wheel, the scrollbar) stops it
following, so you can read in peace; scrolling back to the bottom makes it follow again.

## Keys

| Key | What it does |
|---|---|
| `/` | Open the command menu. |
| Esc | Cancel the current turn, or close an open menu or chooser. With the `stream_json` deep runner, a stop that keeps the session: `/resume` continues it. |
| Ctrl+C | Copy the selected text. With nothing selected one press never ends the app: while the AI works it stops the work like Esc (`Stopped. Press Ctrl+C again to quit.`), when idle it says `Press Ctrl+C again to quit.`, and a second press within 3 seconds quits. |
| Alt+Y | Copy the last AI reply (not Ctrl+Y: the prompt's own redo answers that key first). |
| Ctrl+L | Clear the screen. |
| Enter | Send the message. |
| Shift+Enter | New line. Where the terminal cannot tell Shift+Enter from Enter, use Ctrl+J, Alt+Enter, or type `\` before Enter. |
| PageUp / PageDown | Scroll the transcript. Scrolling up stops it following new output; scrolling back to the bottom resumes. |
| Tab | Show the next job's detail (or complete a command while the menu is open). |
| Alt+D | Show the running job's detail: every action, and every metric result. |
| Alt+C | Show the sources the last turn used. |

The line under the prompt lists only the keys that would do something now, most useful first: Esc while the
AI works, then Alt+D, Alt+C, Tab (only when there is more than one job), PgUp/PgDn and, when nothing runs, Ctrl+C
Quit (copying is a mouse selection or Alt+Y). It shows whole hints and drops the last ones that do not fit, so Esc
is always the one that stays.

Click and drag selects text without holding Shift; Ctrl+C copies it (over SSH too).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QAR_CORPUS_ROOT` | none | The folder the chat grounds answers in, and where it looks for synced quest folders. |
| `QAR_CHAT_HISTORY_DIR` | `~/.quest-ai-runner/conversations` | Where conversations are saved, and read back by `--resume`. |
| `QAR_CHAT_LOG_FILE` | `~/.quest-ai-runner/logs/chat.log` | Where the chat writes its full diagnostic log (the transcript shows only short warning lines). |
| `QAR_STATE_PATH` | `./qar_state.json` | Where the chat remembers your model, representative and `/quest none` choice. |
| `QAR_QUEST_AUTO_MATCH` | `1` | `0` makes "no quest" the default (a person's `/quest auto` still wins). |
| `QAR_REP_NAME`, `QAR_REP_PERSONA_FILE` | none | Default representative name and persona file. |
| `QUEST_BASE_URL`, `QUEST_API_KEY`, `QUEST_TEAM_ID`, `QUEST_TEAM_IDS` | none | Quest credentials, needed to list quests from Quest. |
| `QAR_CONFIG_FILE` | none | A TOML config file, same as `--config`. |

The full list of variables the runner reads is in the module docstring of
[`quest_ai_runner/cli.py`](../quest_ai_runner/cli.py).
