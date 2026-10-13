# Direct tools

A request like "email me today's plan" should not need a deep run. Direct tools are small,
described actions the brain calls **in-process, in one planner step**: the loop runs the call, shows
the planner the receipt, and the planner answers. A deep run, when one does happen, gets the same
catalog as shell commands, so it never has to write its own script for a job a tool already does.

There are two kinds, and the model sees both side by side:

- **Standard tools** ship with the library and are on by default for every deployment whose
  credentials they need are present (all of them need Quest credentials). Two mail tools:
  `send_quest_email`, and `file_email_draft`, which files one email draft into the key owner's
  Approval Queue in the Quest app (`POST /api/email-drafts`); nothing is sent until a person
  approves it there. And the **quest-data tools**, the ONLY way an AI changes a quest's own data,
  one tool per thing it writes: `update_quest_outcome` (the vision statement),
  `update_quest_purpose`, `update_quest_current_state`, `update_quest_preferences`,
  `update_quest_acceptance_criteria`, `update_quest_scope`, `update_quest_goal_statement`,
  `update_quest_completion_criteria`, `add_quest_measurable_outcome`, `create_quest_goal`,
  `add_quest_strategy`, `create_quest_habit`, and for the Experiments section `create_quest_experiment` and `update_quest_experiment` (read or search them first with the read-only `find_quest_experiments`, which matches words across question, context, hypothesis and conclusion; experiments are not quest fields, so they use the section's own routes and are created or changed only when the user asked). Each tool's description carries the writing
  standard for that one field or item, from `core/quest_field_instructions.py` (goals carry a
  nuance per period: day, week, month, quarter, year), so the model reads how to write a good
  value at the moment it chooses the write. Field writes declare `actor="ai"` plus the structured
  `user_asked_for_this_field` verdict, and Quest's AI field-write gate decides whether they apply
  or become an ask for the owner; the goal and habit tools create nothing the user did not ask
  for. A quest-data change is never a code or file edit, and the orchestrator's quest-data ladder
  guard keeps the file-editing deep rungs off that work.
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
| `QUEST_BASE_URL` (or `QUEST_API_URL`), `QUEST_API_KEY` | Enable the standard mail and quest-data tools. A lane configured by file falls back to its `quest_base_url` / `quest_api_key`. |

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

## Offering tools and connectors to Quest Workflows

Quest Workflows can run a step as one action. Quest does not hard-code this runner's tools: the
runner **publishes** them. On its heartbeat, per team and org it serves, the runner PUTs an action
list to `PUT /api/{teams|orgs}/{id}/action-catalogs/quest-ai-runner` (same `qsk_` key, the key's user
must be a member). Quest stores it and the workflow editor lists each entry; the runner republishes
when the list changes and every 4 hours as a keep-alive (Quest hides a catalog not refreshed for 48
hours). Code: `runner/action_catalog.py`.

What is published is decided by a rule, not a list:

- A **standard tool** is published when it acts (`mutates` is true). Read-only tools return text
  nothing in a scheduled step would use, so they stay out. A tool overrides the rule either way with
  `workflow_step`. A **custom tool** (from `QAR_TOOLS_FILE`) is published only when its entry says
  `workflow_step = true`: an operator's tool can do anything, including mail people outside the
  team, so publishing is opt-in. Outward or irreversible tools stay `workflow_step = false` unless
  they carry their own approval step. `ToolSpec.workflow_hidden_params` keeps a parameter out of the
  published form and out of a step's arguments.
- A **connector** (an MCP server in `mcp_servers`) publishes each operation on its **write**
  allowlist (`writable_tools`). Read-side tools never appear.

Name, description and parameters come straight from the tool or server definition. Parameters the
runner fills itself (`defaults`, `context_args` such as `quest_id`) are not asked for; object and
array parameters are entered as JSON text.

When a step runs a published action, Quest queues an ordinary task for the environment that
published it, carrying `requested_action: {publisher, kind, name, action_id, args}`, as the person
who set the action and with the step's persona. The poller recognizes the field after claiming the
task and calls exactly that tool or connector operation with exactly those arguments
(`runner/requested_action.py`): no planner, no deep run, so the step does what it says. The outcome
is reported done or failed on the task, and Quest's existing reconcile marks the step.

### What a step may and may not do

The action runs with the **runner owner's Quest key**, not the key of the person who built the
workflow. Quest's own permission check therefore sees the runner. These checks stand in for it:

- The poller does not claim a task whose `requested_action.publisher` is not `quest-ai-runner`, or
  that is pinned to an environment other than this runner's. A runner with no `env_id` never claims
  a pinned task. Skipped tasks stay queued for the runner they belong to.
- Before running, the runner rebuilds its own published list and requires the task's `action_id`,
  `kind` and `name` to match an entry in it. A read tool, a withheld tool or an unlisted connector
  operation is refused.
- Only arguments named in that action's published form are kept. `quest_id`, `task_id` and any
  hidden parameter always come from the task, never from the arguments.
- If the task names a quest, its author (`created_by`, else `user_id`) must be one of the quest's
  `owner_user_ids`, or Quest must have stamped `requested_action.authorized` (`user_id`, `quest_id`,
  `can_edit: true`, `checked_at`; set server side only, after re-checking the author's edit rights at
  dispatch) for that same author and quest. The stamp is how co-editors run actions. A mismatched
  stamp or `can_edit: false` is refused. Environment pinning uses the task's `env_id`.

Published standard tools: `send_quest_email` (subject and body only; it mails the quest's own
people as the step's persona, never a recipient override or `rep_id`), `file_email_draft` (files one
draft into the Approval Queue; this is the only path to an outside recipient and nothing is sent
until a person approves), the quest field tools and `add_quest_measurable_outcome` (the
`user_asked_for_this_field` claim is hidden, so Quest's AI field-write gate decides and may turn the
write into an ask for the owner). Withheld: `create_quest_goal`, `add_quest_strategy`,
`create_quest_habit`, `create_quest_experiment`, `update_quest_experiment` (each refuses without the
user's own say-so, which a scheduled step cannot assert) and every read tool.
