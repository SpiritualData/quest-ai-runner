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
| `QUEST_TEAM_ID` | yes | the team this lane serves (task claiming/escalation, always required) |
| `QUEST_ORG_ID` | optional | when set, the environment heartbeat registers at ORG scope instead of team scope, making this runner available to every team in the org, not just `QUEST_TEAM_ID` |
| `ANTHROPIC_API_KEY` | yes (for the reference provider) | model calls |
| `QAR_CORPUS_ROOT` | optional | file root for the `FilesAdapter` |
| `QAR_DEEP_WORKING_DIR` | optional | working dir for the subprocess deep-runner |
| `QAR_CLAUDE_PATH` | optional | the deep-runner worker binary (default `claude`) |
| `QAR_STATE_PATH` | optional | signature dedup store (default `qar_state.json`) |
| `QAR_POLL_INTERVAL` | optional | loop cadence in seconds (default 900) |
| `QAR_MAX_MEMORY_PERCENT` | optional | pause pickup when memory usage exceeds this % |
| `QAR_MIN_FREE_MEMORY_MB` | optional | pause pickup when available memory drops below this MB |
| `QAR_MAX_LOAD_PER_CORE` | optional | pause pickup when 1-min load per CPU core exceeds this |
| `QAR_RESOURCE_RESUME_MARGIN` | optional | hysteresis % a tripped limit must clear to resume (default 10) |
| `QAR_RESOURCE_CHECK_INTERVAL` | optional | seconds between re-checks while paused (default 30) |

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

## Resource-aware throttling (overload protection)

The deep-runner spawns a coding agent per task, which is the heaviest thing the lane does. If the
host can't take more (memory nearly exhausted, load climbing), the runner can pause gracefully
instead of thrashing. Disabled by default; opt in by setting any of the `QAR_MAX_MEMORY_PERCENT`,
`QAR_MIN_FREE_MEMORY_MB` (remaining-resource form), or `QAR_MAX_LOAD_PER_CORE` limits — or pass a
`ResourceLimits` on `RunnerConfig.resource_limits` in code.

How it behaves while a limit is tripped:

- **New pickup pauses; nothing is lost.** The runner stops discovering/claiming. Queued tasks
  stay queued on the backend and run on a later scan once resources recover. In-flight tasks are
  never killed, and the heartbeat keeps firing so the backend still sees the env as live.
- **It notices, loudly.** Entering overload logs a WARNING naming the tripped limits and the
  measured values; recovery logs at INFO. Watch `journalctl` (or your log sink) for
  `system overload detected`.
- **It resumes promptly.** In loop mode the runner re-checks every `QAR_RESOURCE_CHECK_INTERVAL`
  seconds (default 30) instead of waiting out a full poll interval. `QAR_RESOURCE_RESUME_MARGIN`
  (default 10%) is the hysteresis: a tripped metric must clear its limit by that margin before
  pickup resumes, so a value hovering at the boundary doesn't flap the lane on and off.
- **Mid-scan protection.** Each task in a batch re-checks before being claimed, so a batch that
  itself pushes the host over the limit defers its remaining tasks to a later scan.

Sampling is stdlib-only (`/proc/meminfo` on Linux, `os.getloadavg` on Unix); installing `psutil`
extends memory sampling to other platforms. A configured limit whose metric this host can't read
is logged once and skipped, never enforced blind.

## Upgrading a running lane

Standing a lane up is the easy half. The half that bites is the second deploy.

**A lane loads its code at process start.** Upgrading the package, editing your consumer, or
`pip install -e` on a working tree changes nothing about the process that is already running: it
keeps executing whatever was in memory at its last restart, silently, for as long as you let it. A
deployment in the wild was found still running five-day-old code after the fixes had landed,
because nothing ever restarted the service. If you take one thing from this page, take this: a
release is not deployed until the lane restarts.

The naive fix has its own failure. A deep run can take many minutes, and a blind
`systemctl restart` in the middle of one throws that work away.

### Verify before you restart

Run these against the code you are about to deploy, as the user the service runs as:

```bash
my_lane.py --check     # exits 0 and prints whoami; read-only, safe against a live lane
my_lane.py --once      # ONE real scan, then exit
```

`--check` proves the config builds and the key authenticates. `--once` proves the lane actually
works: it discovers, claims, runs, and reports one real task. Do not skip it because `--check`
passed. A lane that starts cleanly can still fail on its first deep run.

If the lane's config changed, confirm it changed the way you meant. Build the `RunnerConfig` from
the old and new code and diff every field:

```bash
python -c "
import my_lane
cfg = my_lane.build_config()
for f in sorted(vars(cfg)): print(f, '=', repr(getattr(cfg, f)))
" > /tmp/after.txt
# then the same against the previous version, and: diff /tmp/before.txt /tmp/after.txt
```

Every difference must be one you can explain. This is the cheapest safety net there is.

### Restart without killing work in flight

Ship the idle-guarded restart rather than a bare one:

```bash
scripts/restart_if_idle.sh my-lane.service            # a `systemctl --user` unit
scripts/restart_if_idle.sh my-lane.service --system   # a system unit
```

It restarts the unit unless a deep-run child is alive inside the service cgroup, in which case it
says so and exits 0. Shallow work is deliberately not protected: it finishes in seconds and
survives a restart losslessly, because an unclaimed task stays queued and a task claimed mid-step
is reaped by the backend's stale-task sweep.

Schedule it so a lane can never drift far from the code you released:

```ini
# ~/.config/systemd/user/my-lane-restart.timer
[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
RandomizedDelaySec=300
```

A scheduled refresh is a safety net, not a deploy mechanism. **It will also pick up anything else
sitting in your working tree when it fires**, reviewed or not, so treat an editable install's
working tree as production.

### Then confirm it came up

```bash
systemctl --user is-active my-lane.service
journalctl --user -u my-lane.service -n 60 --no-pager
```

Look for the identity line, a poll cycle completing, and no `ImportError` / `AttributeError` /
`ConfigFileError`. Then leave it one poll interval and check that a real task ran.

### Rollback

Keep this possible before you need it. If your consumer is not in version control, copy it aside
first (`cp -a my_lane.py my_lane.py.bak.$(date +%F)`); restoring is then a copy plus a restart.
Rolling back the *library* is separate, and if several lanes share one install, a library rollback
moves all of them at once.

## Operational notes

- **State store.** `QAR_STATE_PATH` holds the signature dedup store for exactly-once handling.
  Persist it across restarts (e.g. a `ReadWritePaths` dir under systemd). It is gitignored.
- **Concurrency.** `max_concurrent_tasks` (in `RunnerConfig`) bounds how many tasks run at once via
  a thread pool.
- **Isolation.** Task discovery is scoped to the executor user the `qsk_` key authenticates as. Keep
  each lane on its own key/owner so queues stay disjoint.
- **The deep-runner is powerful.** It launches a coding agent in `working_dir`; scope that directory
  and the tool gating to what the lane needs. See [SECURITY.md](../SECURITY.md).
