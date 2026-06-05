# Deployment

The executor lane is a single process: `quest-ai-runner` (the installed CLI) or your own script
around `Poller`. It runs in one of three ways — `--check`, `--once`, or loop forever. Run it
wherever your corpus and the model are reachable.

## Configuration

All configuration is environment-driven (see [`.env.example`](../.env.example)):

| Variable | Required | Purpose |
|---|---|---|
| `QUEST_BASE_URL` | yes | Quest API base URL |
| `QUEST_API_KEY` | yes | executor identity (`qsk_...`) — keep secret |
| `QUEST_TEAM_ID` | yes | the team this lane serves |
| `ANTHROPIC_API_KEY` | yes (for the reference provider) | model calls |
| `QAR_CORPUS_ROOT` | optional | file root for the `FilesAdapter` |
| `QAR_DEEP_WORKING_DIR` | optional | working dir for the subprocess deep-runner |
| `QAR_CLAUDE_PATH` | optional | the deep-runner worker binary (default `claude`) |
| `QAR_STATE_PATH` | optional | signature dedup store (default `qar_state.json`) |
| `QAR_POLL_INTERVAL` | optional | loop cadence in seconds (default 900) |

## Validate first

```bash
quest-ai-runner --check     # exits 0 and prints whoami if the key is valid
```

The CLI degrades visibly: if a required key/url is missing it logs the problem and exits 0 (so a
scheduler doesn't error-spam while a key is still being provisioned).

## Option A — cron (`--once`)

Each tick does one scan and exits. Scheduling/timing is Quest's job (the poller only asks "what's
due now?"), so a frequent, simple cron is all you need:

```cron
# every 5 minutes; load env from a file you keep out of version control
*/5 * * * * set -a; . /etc/quest-ai-runner.env; set +a; /usr/local/bin/quest-ai-runner --once >> /var/log/quest-ai-runner.log 2>&1
```

## Option B — systemd service (loop forever)

`/etc/systemd/system/quest-ai-runner.service`:

```ini
[Unit]
Description=quest-ai-runner executor lane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/quest-ai-runner.env
ExecStart=/usr/local/bin/quest-ai-runner
Restart=always
RestartSec=10
# Hardening (the deep-runner spawns a coding agent — scope it):
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/quest-ai-runner

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now quest-ai-runner
journalctl -u quest-ai-runner -f
```

## Scheduling is Quest's job

There is no separate scheduling plumbing. The poller discovers via
`GET /api/assistant-tasks?status=queued&due_before=<now>`:

- **Run now** → Quest stamps `due = now` → picked up on the next scan.
- **Scheduled for T** → `due = T` → not discovered/claimed until `now >= T`.
- **Recurring** → Quest re-stamps the next `due` after each completion.

So the timing layer is Quest; the runner is timing-agnostic. See
[quest-api-contract.md](quest-api-contract.md).

## Operational notes

- **State store.** `QAR_STATE_PATH` holds the signature dedup store for exactly-once handling.
  Persist it across restarts (e.g. a `ReadWritePaths` dir under systemd). It is gitignored.
- **Concurrency.** `max_concurrent_tasks` (in `RunnerConfig`) bounds how many tasks run at once via
  a thread pool.
- **Isolation.** Task discovery is scoped to the executor user the `qsk_` key authenticates as. Keep
  each lane on its own key/owner so queues stay disjoint.
- **The deep-runner is powerful.** It launches a coding agent in `working_dir`; scope that directory
  and the tool gating to what the lane needs. See [SECURITY.md](../SECURITY.md).
