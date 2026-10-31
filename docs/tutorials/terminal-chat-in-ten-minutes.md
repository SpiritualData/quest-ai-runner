# Run Quest AI Runner in the terminal against a folder in ten minutes

By the end of this tutorial you will have the terminal chat open on a folder of your own notes,
have asked it a question about them, and have closed it and picked the conversation up again. You
need Python 3.10 or newer and a model provider: an Anthropic API key, or a logged-in `claude` CLI.
You do not need a Quest account.

## 1. Install

From a checkout of this repository:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[anthropic,dev]'
quest-ai-runner --help
```

The terminal UI (Textual) is a core dependency, so nothing else is needed for the chat.

## 2. Point it at a folder

Choose any folder of text, markdown or code. The chat reads it and grounds its answers in it.

```bash
export QAR_CORPUS_ROOT=/path/to/your/notes
export ANTHROPIC_API_KEY=...        # skip this if the claude CLI is already logged in
```

## 3. Check that the chat can start

```bash
quest-ai-runner chat --check
```

This checks the model provider and the context store, then exits without opening the chat. If it
reports a problem, fix that first (see [the terminal chat](../how-to/chat.md) for the variables it reads).

## 4. Open the chat and ask a question

```bash
quest-ai-runner chat
```

Wait for `Ready.`, then ask something your folder can answer, such as "Summarize the main
topics in these notes." The AI reads the matching parts of the folder, answers, and says in one
sentence what it looked at (`Looked at 6 sources: ...`). The closing line shows how long the turn
took and which model ran it.

Without Quest credentials the chat still works; it just has no quests to select. Type `/` to open
the command menu and try `/status` to see token usage and speed.

## 5. Leave and come back

Press Ctrl+C or type `/quit`. The chat prints the exact command that reopens the conversation:

```text
Resume this session with:
  quest-ai-runner chat --resume 0123abcd0123abcd0123abcd0123abcd
```

Reopen the most recent conversation for this folder with:

```bash
quest-ai-runner chat --continue
```

Your earlier turns are shown again and new turns append to the same conversation. `quest-ai-runner
chat --resume` opens a searchable list of every saved conversation instead.

## Where next

- [The terminal chat](../how-to/chat.md): every command, key and option.
- [Quickstart](quickstart.md): use the brain from Python and run the executor lane against a Quest backend.
- [Your first lane](tutorial-your-first-lane.md): a working lane from nothing.
