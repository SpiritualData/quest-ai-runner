# The ACP deep runner (opt-in)

`AcpDeepRunner` is a **second** `DeepRunner` implementation. It runs the same bounded, goal-driven
contract as the reference `SubprocessGoalRunner`, but talks to Claude over the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) — a live, bidirectional JSON-RPC
session — instead of shelling out to `claude -p` once and parsing the JSON it prints.

**This is additive.** `SubprocessGoalRunner` is unchanged and remains the default. Nothing switches
to ACP unless a consumer explicitly wires it. The brain cannot tell the two apart: both satisfy
`core.adapters.DeepRunner`, with the same `run_goal` signature, the same `DeepResult`, the same
`EVENT_EXEC` progress stream, and the same human-fork behaviour.

## Why it exists: mid-turn steering

`claude -p` is one prompt in, one blob out. Once it is running, nothing can reach it. A user message
that arrives mid-run can only be folded in at the **next** goal-loop attempt, because that is the
only moment the orchestrator gets control back (see the `_drain_pending` call in its goal loop).

The Claude ACP agent advertises a steering extension
(`InitializeResponse._meta.steering.supported`). Its `_session/steering` request injects a message
into the turn that is **currently running**, delivered at a priority that pre-empts the current
generation — it slots in between a multi-step turn's tool calls rather than queueing behind them.
That is the one capability this adapter buys, and it is wired to the mechanism QAR already has for
mid-run input, `core/inbox.py`'s `InputInbox`.

Nothing else about the deep contract changes. Use `SubprocessGoalRunner` unless you want this.

## Requirements

| Piece | What | Install |
|---|---|---|
| Python client | the `agent-client-protocol` package (imported as `acp`) | `pip install 'quest-ai-runner[acp]'` |
| The agent | `@agentclientprotocol/claude-agent-acp` (Node, Apache-2.0), which wraps Anthropic's Claude Agent SDK | `npm i -g @agentclientprotocol/claude-agent-acp` |
| Node | **>= 22** — the agent's `engines` requirement | see below |
| Auth | none to add: the agent reuses whatever Claude Code / Agent SDK login is already active, the same subscription auth `SubprocessGoalRunner`'s worker uses | — |

The Python import is **lazy**: it happens inside `open_agent_connection`, so importing
`quest_ai_runner.adapters` never requires the extra, and a deployment that does not use ACP pays
nothing. The npm agent is not pip-installable, which is why the `[acp]` extra is not part of `[all]`.

### Node >= 22 is not optional, and your box's default probably isn't it

A distro-packaged Node is frequently older than 22, and on a shared machine you often cannot upgrade
the system one (other tooling depends on it). So the Node used to launch the agent is **config**,
not a bare PATH lookup:

1. `AcpConfig.node_path` — an explicit path, highest precedence.
2. `QAR_ACP_NODE_PATH` — the env fallback, for a deployment that configures by environment.
3. `node` on `PATH` — the default.

Whatever is chosen is probed with `node --version` **before** the agent is spawned. Too old, and the
run fails immediately with a `DeepResult` naming the version it found, the path it found it at, and
the knob to set — rather than dying inside the child process with an engine warning or a syntax
error. Install a newer Node however this deployment prefers (nvm, a vendor build, a container base
image); nothing here assumes an install method, and nothing touches the system default.

The chosen Node's directory is also prepended to the child's `PATH`, so anything the agent shells
out to resolves to the same Node that passed the check.

The agent program itself resolves the same way: `AcpConfig.agent_command`, then
`QAR_ACP_AGENT_COMMAND`, then `claude-agent-acp` on `PATH`. An npm bin shim is resolved through its
symlink to the `.js` entry point and launched as `<node> <entry>`, because the shim's shebang would
otherwise pick up whichever `node` is first on `PATH` — usually the too-old one.

## Wiring it

Selecting a deep runner is just `RunnerConfig.deep_runner`, exactly as it is for the subprocess one:

```python
from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.adapters import AcpConfig, AcpDeepRunner
from quest_ai_runner.core.inbox import InMemoryInbox

inbox = InMemoryInbox()          # the same inbox the interface pushes user messages into

cfg = RunnerConfig(
    # ... your retrieval, provider, quest connection ...
    deep_runner=AcpDeepRunner(AcpConfig(
        working_dir="/path/to/corpus",
        node_path="/path/to/node22/bin/node",   # or leave None and set QAR_ACP_NODE_PATH
        steering_inbox=inbox,
        steering_conversation_id="conv-123",
    )),
    input_inbox=inbox,
)
```

`AcpConfig` mirrors `SubprocessConfig` field-for-field wherever the two mean the same thing
(`working_dir`, `context_preamble`, `skip_permissions`, `allowed_tools` / `disallowed_tools`,
`timeout_seconds`, `extra_path_dirs`), so moving a consumer between the two runners is a one-line
change. Capability reporting works through the same path too: `derive_capabilities` reads
`deep_runner.cfg.web_enabled()`, and `AcpConfig.web_enabled()` derives web access from the actual
tool gating, exactly as `SubprocessConfig` does.

## Session lifecycle

**One agent process and one ACP session per `run_goal` call**, torn down when it returns — the same
lifetime a `claude -p` spawn gets today.

That is deliberate. The orchestrator's goal loop calls `run_goal` once per attempt and never signals
"this subgoal is finished", so a session held across calls would have no defined moment to close and
would leak a Node process per retry. Continuity across attempts already lives a level up: the loop
feeds each retry a brief refined with what fell short.

## Steering: the two routes in

Both go through `_session/steering` on the live session.

**The inbox route** (hands-off). Configure `steering_inbox` + `steering_conversation_id`. While the
turn is in flight, a background task drains that conversation every `steering_poll_seconds` and
injects whatever it finds. This is the same `InputInbox` the orchestrator drains between attempts,
so an interface that already calls `inbox.push(conversation_id, message)` needs no new integration.

**The direct route.** `AcpDeepRunner.steer(message, run_id=None)` injects from any thread (it hands
the coroutine to the run's own event loop). With no `run_id` it goes to every live run — usually
exactly one. `active_runs()` lists them.

**Nothing is silently swallowed.** The steering request is sent with
`_meta.steering.idleBehavior = "promptRequired"`, which is the opt-in that stops an already-settled
turn from starting a *new detached* turn (unbounded work outside the goal loop and outside this
run's timeout). If the turn has settled, the agent answers `promptRequired`, the message is pushed
back to the inbox for the orchestrator's own between-attempts drain, and the steering channel closes
for the rest of the run so a returned message is never re-offered on the next poll tick. An agent
that does not advertise steering is never drained at all.

## What the stream looks like

`session/update` notifications are translated into the `EVENT_EXEC` ticks QAR already emits for deep
runs — same event type, same `data["run_id"]`, same `data["phase"]` convention, same one-line
texture — so every existing consumer renders this unchanged.

| ACP `session/update` | `phase` | Rendered as |
|---|---|---|
| `agent_message_chunk` | `message` | the text (bounded), and accumulated into the result |
| `agent_thought_chunk` | `thinking` | `[thinking] …` |
| `tool_call` | `tool_call` | `$ pytest -q`, `Read: docs/README.md`, `Using …` |
| `tool_call_update` (pending/in_progress) | `tool_progress` | `in_progress: <title>` |
| `tool_call_update` (completed) | `tool_result` | `completed: <title>` |
| `tool_call_update` (failed) | `tool_error` | `failed: <title>` |
| `plan` | `plan` | `Plan: 4 task(s), 1 done` + the entries in `data` |
| `current_mode_update` | `session` | `Permission mode: …` |
| `usage_update` | — | accounted as cost on the result, not shown |
| `user_message_chunk`, `available_commands_update`, `config_option_update`, `session_info_update` | — | dropped (our own echo, and startup chatter) |

A tool finishing deliberately does **not** use the phase strings `core/guard.py` treats as terminal
(`done` / `completed` / `failed`): a completed `Read` is not a completed subgoal, and reusing those
would let one tool call mark the whole deep task succeeded. Only the run's own final tick is
terminal (`done` or `error`).

Silence longer than about 10 seconds while the turn is working emits a `heartbeat` tick, the same
liveness rule the subprocess runner's session monitor follows.

The **result** is the last agent message, matching what `claude -p --output-format json` reports as
`result`; everything before it is narration the live stream already showed. `stop_reason: end_turn`
is this runner's exit-code-0 — with the same guard against a silent no-op (a clean end that produced
no text at all is a failure, not a hollow success). Every other stop reason comes back `met=False`
with the reason written out.

## Permissions and the human fork

The agent asks for permission via `session/request_permission`. Answers come from the **existing**
QAR permission model — there is no second policy system:

1. The tool is identified from the structured `toolCall._meta.claudeCode.toolName` field, never from
   the agent-composed `title` (hard rule #3: no control flow gated on words a model wrote).
2. In `disallowed_tools` → **reject**. This beats auto-approval.
3. `allowed_tools` pinned and the tool is not on it → **reject**. If the tool could not be
   identified at all, this fails **closed**.
4. `skip_permissions=True` (the default, the autonomous equivalent of
   `--dangerously-skip-permissions`) → **allow**.
5. Otherwise it is a human decision. With an `EscalationSink` wired (`AcpConfig.escalation`), the ask
   becomes a real decision-request with `default_on_silence="hold"`, the tool is denied so the turn
   cannot proceed past the unapproved step, and `run_goal` returns
   `DeepResult(met=False, decision_id=...)` — the exact `needs_you` contract the `QAR-ESCALATED:`
   marker gives the subprocess runner. With no sink wired, the tool is denied (nobody could be
   asked). A sink that raises degrades to a denial.

The options themselves are chosen by their structured `kind` (`allow_always` → `allow_once` for an
approval, `reject_once` → `reject_always` for a denial), never by their display name.

The `QAR-ESCALATED: <id>` marker still works too: a worker that raises a decision the prose way is
honored identically.

## Known limits

- **`max_turns` is not enforced.** ACP has no turn budget and the Claude ACP agent exposes none. The
  argument is accepted for interface parity. The real bounds are the wall-clock `timeout_seconds`
  here (defaulting to the shared `QAR_DEEP_TIMEOUT_SECONDS` floor) and the orchestrator's own goal
  loop, which is where the bound effectively lived for both runners anyway.
- **No filesystem or terminal capability is advertised to the agent.** It does its own file and
  shell work in its own process, as `claude -p` does.
- **The child does not inherit our Claude session or API key** (`CLAUDECODE`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` are dropped), matching `SubprocessGoalRunner`. A deployment that wants
  API-key billing for deep runs needs a change in both runners, not just this one.

## Failure modes, all reported not raised

Like every `DeepRunner` here, this one never raises: a missing `[acp]` extra, a missing agent
program, a too-old Node, a failed handshake, a session created without an id, a protocol error, a
turn that blows its timeout — each comes back as a `DeepResult(met=False, error=...)` written for
the human reading the failed task.

## Tests

`tests/test_acp_deep_runner.py` runs fully offline: the whole SDK/subprocess surface is behind one
module-level seam (`open_agent_connection`), which the tests replace with a scripted fake
connection. No package import, no process, no auth, no network.
