# Direct tools

A request like "email me today's plan" should not need a deep run. Direct tools are small,
described actions the brain calls **in-process, in one planner step**: the loop runs the call, shows
the planner the receipt, and the planner answers. A deep run, when one does happen, gets the same
catalog as shell commands, so it never has to write its own script for a job a tool already does.

There are two kinds, and the model sees both side by side:

- **Standard tools** ship with the library and are on by default for every deployment whose
  credentials they need are present. Today: `send_quest_email` (needs Quest credentials).
- **Custom tools** are yours, declared in a TOML file: any command or Python function, described
  with when to use it and when not to.

## How the model chooses

Every tool carries four things the model reads: a `description`, `when_to_use`,
`when_not_to_use`, and its argument schema. The planner gets a fifth action, `"tool"`, with
`"tool_calls": [{"name": ..., "args": {...}}]`.

The catalog is not dumped into every prompt. With 8 tools or fewer the planner sees them all; above
that it sees only the 5 most relevant to the request (BM25 over each tool's name, keywords,
description and when-to-use text), plus a note that more exist and can be searched with a read spec
`{"tools": "<what you need>"}`. The same selection feeds the deep-run brief.

Safeguards in the loop:

- A call that already succeeded this turn is never run twice.
- A mutating call made during brainstorm mode is held, like any other act.
- A mutating call is recorded as an execution fact, so the claim verifier knows what really ran and
  the turn never escalates to a deep run to redo work a tool already did.
- If the step budget ends on a tool step, the turn answers with the receipts it has; it does not
  escalate to a deep run.

## Configuration

| Env var | Effect |
|---|---|
| `QAR_STANDARD_TOOLS` | `0` turns every standard tool off. Default on. |
| `QAR_TOOLS_FILE` | One or more TOML files (`:`-separated) of custom tools and standard-tool overrides. |
| `QUEST_BASE_URL` (or `QUEST_API_URL`), `QUEST_API_KEY` | Enable `send_quest_email`. A lane configured by file falls back to its `quest_base_url` / `quest_api_key`. |

A consumer building its own `RunnerConfig` can instead pass a ready `ToolRegistry` as
`RunnerConfig.tool_registry` (from `quest_ai_runner.core.tools`); that wins over the env.

A broken tools file never stops a lane: it is logged and skipped. Check what a lane would see with:

```bash
python -m quest_ai_runner.tools list                        # the whole catalog
python -m quest_ai_runner.tools list --query "email a donor" # what this request would surface
```

## Declaring a custom tool

```toml
[[tool]]
name = "queue_email_for_review"
description = "File an email draft into our review queue. Nothing is sent until a person approves it."
when_to_use = "Mail to anyone outside the team, or any message a person should read before it goes out."
when_not_to_use = "An internal update to the quest's own people that can go out now: use send_quest_email."
command = ["{python}", "scripts/queue_draft.py", "create", "--json"]
args_mode = "flags"          # or "stdin_json"
mutates = true               # default true; false marks a read-only tool
keywords = ["email", "draft", "review", "external"]
defaults = { owner = "ops" } # used when the model gives no value
context_args = { }           # e.g. { quest_id = "quest_id" } fills an arg from the turn's context
timeout_seconds = 30
# cwd = "/path/to/run/in"    env = { SOME_VAR = "$OTHER_VAR" }

[tool.parameters]
type = "object"
required = ["to", "subject", "body"]

[tool.parameters.properties.to]
type = "array"
items = { type = "string" }
description = "Recipient addresses."

[tool.parameters.properties.subject]
type = "string"

[tool.parameters.properties.body]
type = "string"
```

`name`, `description`, `when_to_use` and `when_not_to_use` are all required: the model chooses by
them, so a tool without them is rejected at load.

**Command tools.** `{python}` expands to the running interpreter and `$VAR` to the environment.
With `args_mode = "flags"` each argument becomes `--name value` (underscores become dashes; set
`flag = "--other"` on a property to rename it), an array repeats the flag, and a true boolean is a
bare flag. With `"stdin_json"` the arguments arrive as one JSON object on stdin. Exit 0 is success
and anything else a failure; stdout is the result text, parsed into `data` when it is JSON. The
command also gets `QAR_TOOL_QUEST_ID` and `QAR_TOOL_TASK_ID` in its environment.

**Python tools.** `handler = "package.module:function"` instead of `command`. The function takes
`(args: dict, ctx: ToolContext)` and returns a `ToolResult`, a dict, or a string.

## Adjusting a standard tool

A `[standard.<name>]` table tunes a standard tool without redefining it:

```toml
[standard.send_quest_email]
defaults = { quest_id = "<your team-wide quest id>" }   # where "email me" goes with no quest open
when_to_use_extra = "With no quest open it sends through the team quest."
when_not_to_use_extra = "Anyone outside the team: use queue_email_for_review."
keywords = ["notify"]
# enabled = false   # remove it entirely
```

## Calling a tool from a deep run

The deep-run brief lists the relevant tools with the exact command for each:

```bash
python -m quest_ai_runner.tools call send_quest_email \
  --args '{"subject": "Weekly plan", "body": "..."}' --quest <quest id>
```

`--args-file PATH` (or `-` for stdin) takes the JSON from a file. Exit code 0 is success, 1 a failed
call (the reason on stderr), 2 bad arguments.
