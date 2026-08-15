# Live channels (opt-in): a real-time, two-way chat lane

QAR's task poller and interactive terminal session cover "run a queued task" and "someone is
typed in at a terminal." Neither covers a **live conversation over a messaging app** — someone
texting the assistant on their phone and expecting a reply in the same chat. This is that lane:
a generic `core.adapters.ChannelTransport` interface plus a `runner.channel_runner.ChannelRunner`
loop, with a reference transport, `adapters.openclaw_channel.OpenClawChannel`, wrapping the
generic `adapters.mcp_client.MCPClient` to talk to [OpenClaw](https://github.com/openclaw/openclaw)
(MIT) over MCP.

**This document is two things:**
1. A **checklist** an operator MUST verify before pointing this bridge at a real OpenClaw Gateway.
   Nothing in this repo installs, configures, or runs OpenClaw — that is entirely the operator's
   responsibility, and every requirement below exists because of a real, documented risk.
2. Reference docs for the `channel` CLI subcommand and its config surface.

**Status:** the mechanism (transport interface, OpenClaw bridge, the runner loop, message
authorization/dedup/session-folding, the terminal-reply guarantee) is built and tested against
fakes — see `tests/test_openclaw_channel.py`, `tests/test_channel_runner.py`. **No live end-to-end
run against a real OpenClaw Gateway has been performed**; that needs a real channel bot token
(Telegram/WhatsApp/etc.) that only a human operator can create. Treat every OpenClaw-side detail
below (exact tool argument names, event payload shapes) as the best-documented assumption at
authoring time, not a pinned fixture — see the "Unverified assumptions" section.

## Why the checklist is non-negotiable

OpenClaw carries a confirmed 2026 vulnerability, **CVE-2026-25253** (CVSS 8.8): an attacker-spoofed
`gatewayUrl` could exfiltrate the Gateway auth token and lead to remote code execution, affecting
an estimated ~40,000 exposed instances before it was fixed in **v2026.1.29**. OpenClaw also has a
broader documented history of malicious-skill credential-exfiltration and prompt-injection
incidents. None of that is this bridge's code — it is what OpenClaw ITSELF must be locked down
against before this bridge (or anything else) talks to it.

## Operator checklist — verify every line before connecting

- [ ] **OpenClaw is pinned at v2026.1.29 or newer.** Confirm the running version explicitly; do
      not assume a container tag or package manager gave you a patched build.
- [ ] **No skills/plugins are enabled** in OpenClaw's own config.
- [ ] **No cron/scheduled jobs are enabled** in OpenClaw's own config.
- [ ] **No browser-automation capability is enabled** in OpenClaw's own config.
- [ ] **No agent/model is configured in OpenClaw at all.** OpenClaw is pure message relay; QAR
      (via this bridge) is the brain. If OpenClaw's config has any field naming an LLM, an API key
      for one, or an "agent"/"assistant" mode, that is wrong — disable it.
- [ ] **The Gateway is bound to localhost only** (`127.0.0.1` / a Unix socket), never a public IP,
      a `0.0.0.0` bind, or a URL reachable from outside the host. This bridge always talks to it
      over a local stdio subprocess (`openclaw mcp serve`), never over the network — a
      network-reachable Gateway is not something this bridge needs and should not exist.
- [ ] **OpenClaw holds only its own credentials**: channel bot tokens (the Telegram bot token, the
      WhatsApp session, etc.) and its own Gateway token. It must NEVER be given `QUEST_API_KEY`,
      any model provider API key (`ANTHROPIC_API_KEY`, etc.), or any access to this corpus. If
      OpenClaw's config or environment contains any of those, remove them — OpenClaw has no
      legitimate use for them and every one of them is a credential this bridge deliberately never
      hands it (see "What this bridge does and does not send OpenClaw" below).
- [ ] **The Gateway token lives in a file this process can read, and nowhere else.** Point
      `QAR_OPENCLAW_TOKEN_FILE` (or `OpenClawChannelConfig.token_file`) at it. Never paste the
      token into an env var, a command-line flag, a log line, or this repo.
- [ ] **`QAR_CHANNEL_ALLOWED_SENDERS` is set to the exact, real sender id(s) that should reach this
      runner** — and nothing else. The default is empty, which denies every sender; leaving it
      empty by accident just means nobody gets a reply, not an open door, but an accidentally
      OVER-broad list (a wildcard, a channel-wide id) would be a real exposure. Set it narrow.

None of this is enforced by QAR at runtime — QAR has no way to inspect or control a separate
OpenClaw process's own config. This checklist is the actual control.

## What this bridge does and does not send OpenClaw

- **Sends:** MCP tool calls (`events_wait`, `messages_send`, `attachments_fetch`) over a local
  stdio pipe to the `openclaw mcp serve` process this runner itself spawns, plus the Gateway token
  FILE PATH as a `--token-file` argument (the token bytes are read by the `openclaw` process from
  that file directly; this bridge's own process never holds the token as a string in memory or logs
  it).
- **Never sends:** `QUEST_API_KEY`, any model provider API key, corpus contents, or anything about
  this deployment's internal state beyond the reply text itself. OpenClaw only ever sees the text
  QAR decides to send back on a chat.
- **Never calls `permissions_respond`.** OpenClaw's own approval surface
  (`permissions_list_open` / `permissions_respond`) is not used by this bridge at all — there is no
  method on `OpenClawChannel` that calls it (`tests/test_openclaw_channel.py::
  test_bridge_never_calls_permissions_respond` pins this structurally). Approvals for anything QAR
  itself needs a human for go through QAR/Quest's own decision-request mechanism (`EVENT_DECISION`,
  relayed to the chat as a message — see below), never OpenClaw's.

## What the runner does with a message

1. **Receive.** `ChannelRunner.run_once()` (looped by `run_forever()`) calls
   `transport.receive(timeout=...)`, a long-poll. A transport error is logged and backed off; the
   loop never dies (mirrors `Poller._fast_lane_loop`'s shape).
2. **Authorize.** Every message's `sender_id` is checked against `channel_allowed_senders` — a
   plain membership test against operator config, never a decision based on anything a model wrote.
   An empty allowlist (the default) denies everyone. Rejected: logged, zero replies, zero
   orchestrator calls.
3. **Dedup.** Keyed on `"<channel>:<message_id>"` via the same `StateStore`
   (`runner/state_store.py`) the task poller uses. A redelivered event runs at most once.
4. **Session / fold-in.** One in-flight turn per `chat_ref` at a time. A message that arrives while
   a turn for that chat is already running is folded into the running turn via `core.inbox.
   InputInbox` (`pending_inputs=lambda: inbox.drain(chat_ref)`) instead of starting a second,
   concurrent turn.
5. **Run.** Otherwise the message is submitted to a bounded worker pool and runs one turn via
   `Orchestrator.run_stream(...)`.
6. **Reply — the terminal guarantee.** Every turn sends EXACTLY ONE terminal reply: the final
   answer, a decision-relay, or a plain error message. Silence is the one outcome that must never
   happen — even an unexpected exception from the orchestrator itself still produces a reply
   (`runner/channel_session.py`'s `ChannelSink`, `runner/channel_runner.py`'s `_run_turn`).
7. **Decisions are relayed, never auto-resolved.** An `EVENT_DECISION` (the orchestrator raising a
   human-only confirm) is sent to the chat as a message, and that IS the turn's terminal reply.
   **This lane never calls the Quest decision-request "resolve" endpoint from a channel reply** —
   auto-resolving a decision from a chat message is a separate trust decision, out of scope here.
   A human resolves it in Quest, the normal way.

## The `channel` CLI subcommand

```
quest-ai-runner channel            # run forever (the live lane)
quest-ai-runner channel --once     # one receive+dispatch pass, then exit (testing)
quest-ai-runner channel --check    # validate config, then exit
```

Env vars (see `cli.py`'s module docstring for the authoritative list, kept in sync with this):

| Var | Meaning | Default |
|---|---|---|
| `QAR_CHANNEL_PROVIDER` | `"openclaw"` wires an `OpenClawChannel`. Unset = no transport. | unset |
| `QAR_OPENCLAW_TOKEN_FILE` | **Required** for the openclaw provider: path to the Gateway token file. | — |
| `QAR_OPENCLAW_COMMAND` | The `openclaw` binary. | `openclaw` |
| `QAR_OPENCLAW_ARGS` | Extra args appended after `mcp serve` (space-separated). | (none) |
| `QAR_OPENCLAW_CWD` | Working dir for the spawned subprocess. | process cwd |
| `QAR_OPENCLAW_CALL_TIMEOUT` | Per-call timeout (seconds) for tools other than `events_wait`. | `30` |
| `QAR_CHANNEL_NAME` | The channel's name / dedup-key prefix. | `openclaw` |
| `QAR_CHANNEL_ALLOWED_SENDERS` | Comma-separated sender ids. Empty = deny all. | (empty) |
| `QAR_CHANNEL_ACK_AFTER_SECONDS` | Seconds before a "still working" ack, if the turn runs long. `<= 0` disables it. | `15` |
| `QAR_CHANNEL_PROGRESS_MIN_SECONDS` | Minimum seconds between two milestone sends in one turn. | `20` |
| `QAR_CHANNEL_TURN_TIMEOUT_SECONDS` | Outer wall-clock safety net per turn. `<= 0` disables it. | `900` |
| `QAR_CHANNEL_STATE_PATH` | Dedup state file. Unset = in-memory only (no persistence across restarts). | unset |

A consumer that wants finer control (a non-OpenClaw `ChannelTransport`, programmatic config) builds
`RunnerConfig` directly and sets `channel_transport` / `channel_allowed_senders` / etc. itself, then
constructs `runner.channel_runner.ChannelRunner(cfg)` — the env-driven CLI path above is a
convenience, not the only way in.

This is its own process/entry point, separate from `poller.py`'s background scan and from
`TaskExecutor`: a chat message has no Quest task id to claim/PATCH, so it is never folded into
either.

## Adding a new channel

Because OpenClaw itself exposes every channel it has configured (WhatsApp, Telegram, Discord,
Slack, Google Chat, Signal, ...) through the SAME MCP server, **a new channel is OpenClaw-side
configuration, not new QAR code.** Configure the connector in OpenClaw; this bridge picks up
whatever conversations OpenClaw relays, tagged with the channel's own name in each event (carried
on `InboundMessage.raw` for a consumer that wants to branch on it).

A consumer that wants a channel OpenClaw does not support writes its own `ChannelTransport`
(`core.adapters.ChannelTransport` / `ChannelTransportBase`) — the runner only depends on that
interface, not on OpenClaw specifically.

## Unverified assumptions (no live OpenClaw instance was available at authoring time)

`adapters/openclaw_channel.py` was built against OpenClaw's **documented tool names and
semantics** (`events_wait`, `messages_send`, `attachments_fetch`, the 30s default / 300s max
long-poll window), but the exact JSON **payload shapes** those tools return were not available to
pin against a real fixture. The parsing is deliberately tolerant (several plausible key spellings
per field — `conversation_id`/`chat_id`/`chat_ref`, `sender_id`/`from`, etc. — see
`_message_from_event`/`_first` in the module) and degrades to a clearly-logged skip rather than
raising or fabricating data on an unrecognized shape. Before relying on this in production:

1. Run `openclaw mcp serve --token-file <path>` by hand against a real, locked-down instance and
   inspect one real `events_wait` response and one real `messages_send` response.
2. Adjust `_message_from_event` / `_parse_json_list`'s key candidates (or the tool argument names
   in `receive`/`send`/`_fetch_attachment`) if the real shapes differ.
3. Add a fixture-based test alongside the existing scripted ones once real payloads are known.

## Tests

- `tests/test_openclaw_channel.py` — `OpenClawChannel` against a fake `MCPClient`-shaped object:
  event translation, attachment fetch, never-raise on a raising fake, the `permissions_respond`
  boundary.
- `tests/test_channel_runner.py` — `ChannelRunner` + `ChannelSink` against fake transports and a
  fake orchestrator: authorization (including the empty-allowlist fail-closed case), dedup,
  mid-turn folding into `InputInbox`, the one-reply-always guarantee (including when the
  orchestrator raises), and the `ChannelSink` messaging policy (chatter dropped, milestones
  throttled, decisions terminal, the ack, empty-answer fallback).
- `tests/test_state_store_extraction.py` — the `StateStore` extraction (`runner/state_store.py`)
  is mechanical: `poller.StateStore` and the channel runner's store are the literal same class.

Run `python -m pytest -q` from the repo root — all offline, no network, no API key.
