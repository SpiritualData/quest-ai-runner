# A lane is a config file, not a program

An executor lane used to need a small Python consumer. Not for anything lane-specific: for
renaming three environment variables, pinning two `QAR_*` knobs, reading one JSON file, and
building one client. Three lanes each wrote that, and each wrote it slightly differently.

Everything below is now expressible in `qar.toml`, so a lane is:

```ini
ExecStart=/path/to/venv/bin/quest-ai-runner poll --config /path/to/qar.toml
```

## The fields that removed the need for Python

| Field | Replaces |
| --- | --- |
| `env_files = [...]` | `load_env_file(...)` before `build_config`, plus any fallback-file logic |
| `[env_aliases]` | a hand-written bridge from a deployment's own `.env` names onto this library's |
| `[env]` | `os.environ.setdefault(...)` for every `QAR_*` knob, including the nested `OrchestratorConfig` ones no top-level TOML field reaches |
| `quest_folder_map_file` | reading a `{quest_id: folder}` JSON file in Python |
| `context_sources_file` / `context_sources_map` | per-card context-source specs a backend cannot carry yet |
| `[drive_comments_auth]` | constructing a `DriveComments` client from a service-account credential |
| `state_path`, `lane_label` | `run_lane(state_path=..., lane_label=...)` |

## Precedence, top to bottom

1. The **process environment** (a systemd unit's own `Environment=`) always wins.
2. `env_files`, in the order listed: earlier files win, later ones are fallbacks.
3. `env_aliases`, then `env`.
4. The config file's own fields, for anything no environment variable set.

Every step is **truthy**-checked, not presence-checked. A lane whose `.env` documents optional
credentials as blank lines (`QUEST_API_KEY=`, meaning "fall back to the shared file") is a real
shape, and a presence check reads those blanks as already-set: the fallback never applies and the
lane starts unconfigured with nothing in the log to say why. That case is exactly why one consumer
carried a hand-written bridge helper with a four-line comment explaining itself.

## Worked example

A lane sharing a credential with another lane, but polling its own team:

```toml
lane_label = "cantr"
runner_label = "cantr-dev-server"
corpus_root = "/srv/cantr"
state_path = "/srv/cantr/state/qar_state.json"

# This lane's own credentials first, the shared file as a fallback.
env_files = ["/srv/cantr/.env", "/srv/shared/.env"]

[env_aliases]
QUEST_BASE_URL = "QUEST_API_URL"
QUEST_TEAM_ID = "CANTR_TEAM_ID"        # its own team, not the shared one
QAR_DECISION_ASSIGNEE = "OWNER_USER_ID"

[env]
QAR_CONTEXT_PREAMBLE_FILE = "/srv/cantr/context_preamble.md"
QAR_MAX_PARALLEL = "3"
```

## What still needs Python

A live object with real behavior: a custom `ContextAssembler`, an `EscalationSink`, a
`rep_sync_resolver` that carries a callback. Those are code, and they belong in a consumer (or,
when a second lane would want them, in this library -- see hard rule #4 in `CLAUDE.md`). Everything
that is only *description* belongs in the file.
