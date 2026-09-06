# Your first lane

This walks from nothing to a working executor lane, then adds the things a real lane needs — a
persona roster, a corpus, a deep-run preamble, a folder map — each as a few lines of config, not
code. By the end, a lane is **config plus one call**, not a Python program.

## What a lane is

A lane is one process that repeatedly does four things against one Quest team:

```
poll  ->  claim  ->  run  ->  report
```

It asks Quest "what's due for me right now", claims a task so nothing else picks it up, runs it
through the brain (grounding, planning, and — for real work — a bounded deep agent run), and
reports the result back (`done`, `needs_you` with a decision attached, or `failed`). `Poller` is
that loop; `runner.lane.run_lane` is the `--check` / `--once` / loop-forever entry point around it.

## What you do NOT own

Three separate consumers of this library each wrote their own version of the following before it
moved into the library. If you find yourself writing any of these, stop — it's a bug, not a
feature, and the thing you need almost certainly already exists:

- **The CLI / loop shape** (`--check`, `--once`, loop-forever, logging setup). That's
  `runner.lane.run_lane` — call it, don't rewrite it.
- **`.env` file loading.** That's `runner.lane.load_env_file` (which `run_lane` already calls for
  you when you pass it an `env_file`).
- **Persona resolution** (a registry of ids/names to skill folders, plus a policy for picking one
  from a task — a structured field, an LLM-judged explicit ask, domain-card dominance, or an
  unknown id becoming a real skill). That's `RunnerConfig.personas` /
  `runner.personas.build_persona_resolver` — see [personas.md](personas.md).

Everything else — your Quest credentials, your corpus, your persona's voice, which quest folders
sync where — is genuinely yours, and it lives in `RunnerConfig` (built from environment variables,
a TOML file, or both together).

## The minimal working version

A lane's `RunnerConfig` can be built entirely from a TOML file plus your Quest credentials. Put
this in `qar.toml`:

```toml
team_id = "team_1"
runner_label = "my-first-lane"
```

And your credentials in the environment (never in the file, unless the file itself is kept out of
version control and off shared disks — see the note in `writing-a-consumer.md`'s file-based-config
section):

```bash
export QUEST_BASE_URL=https://api.example.org
export QUEST_API_KEY=qsk_...          # keep this secret
export QAR_CORPUS_ROOT=/path/to/your/corpus
export ANTHROPIC_API_KEY=sk-ant-...   # or run keyless — see quickstart.md
```

Then the entire entry point is:

```python
#!/usr/bin/env python3
"""My first quest-ai-runner lane."""
from quest_ai_runner import load_config
from quest_ai_runner.runner.lane import run_lane

def main(argv=None) -> int:
    return run_lane(
        argv,
        prog="my-first-lane",
        description="My first quest-ai-runner lane.",
        lane_label="my-first-lane",
        log_name="my-first-lane",
        build_config=lambda: load_config("qar.toml"),
    )

if __name__ == "__main__":
    raise SystemExit(main())
```

```bash
python my_lane.py --check   # validate the key + identity, then exit
python my_lane.py --once    # one scan then exit (good for cron)
python my_lane.py           # loop forever (good for a systemd service)
```

That's it — see [`examples/minimal_lane.py`](../examples/minimal_lane.py) and
[`examples/qar.toml`](../examples/qar.toml) for this exact lane, runnable as-is. Compare its size
to the ~500-870 line hand-rolled consumers this replaces (each independently wrote its own
`--check`/`--once`/loop driver, and one of them also wrote its own persona-resolution machinery
from scratch) — the contrast is the whole point.

If you'd rather stay purely env-driven (no file at all), drop the `build_config=lambda: ...` above
to `build_config=load_config` and set everything via `QUEST_*`/`QAR_*` environment variables
instead — see `writing-a-consumer.md`'s full env-var reference. The two are freely mixable: a
`QAR_*` env var always wins over the same field in the file.

## Adding what a real lane needs

Everything below is still config — add it to `qar.toml` (or the matching env var) as you need it,
with no code change to `my_lane.py` above.

### A corpus

```toml
corpus_root = "/path/to/your/corpus"
```

or `QAR_CORPUS_ROOT=/path/to/your/corpus`. This grounds the brain's reads/greps in your files, and
becomes the deep runner's working directory unless you override that separately
(`QAR_DEEP_WORKING_DIR`).

### A deep-run preamble

Org or persona doctrine to prepend to every deep-run brief — "you are executing a task for this
team; ground on the corpus; surface any human-only step as a decision-request", or similar:

```toml
context_preamble = "You are executing a task for this team. Ground on the corpus; surface any human-only step as a decision-request."
```

For anything longer than a couple of sentences, write it to a file instead and point
`QAR_CONTEXT_PREAMBLE_FILE` at it — multi-line org doctrine is miserable to maintain as a single
TOML string or environment variable line.

### A persona roster

Two shapes, both expressible as one `[personas]` table — see [personas.md](personas.md) for the
full reference:

**Structural** (the task already names its owner — a Quest AI rep running as a real team member):

```toml
[personas]
skills_root = "/srv/skills"                 # one <slug>/SKILL.md folder per rep
registry_file = "/srv/state/rep_slugs.json" # {"<user id>": "<slug>"}
auto_register = true                        # an unknown rep becomes a real invocable skill
cache_dir = "/srv/state/rep-skill-cache"    # ...or, with auto_register off, a per-id folder
```

**Character** (the task is prose from a human, and a persona activates only when asked for):

```toml
[personas]
skills_root = "/srv/skills"
registry_file = "/srv/skills/characters.json"  # {"<name>": {"rep_id": ..., "skill": ..., "aliases": [...]}}
llm_explicit_ask = true      # "have Sage do it" -> Sage (needs a model_provider, wired separately)
card_activation = true       # dominant domain cards -> that character; a bare mention never counts
cards_dir = "/srv/skills/.cards"
```

Either way, `rep_sync_direction` controls whether the sync also pushes local `SKILL.md` edits back
up to Quest:

```toml
rep_sync_direction = "both"   # "pull" (default) | "push" | "both"
```

### A folder map

Ground a specific goal/quest's work in its own local folder (research, drafts, code) instead of the
lane's generic corpus root, and keep that folder's `QUEST_SYNC.md` in sync with the quest's state:

```bash
export QAR_QUEST_FOLDER_MAP='{"quest_1625d9f47a06": "/srv/corpus/some_quest"}'
export QAR_QUEST_FOLDER_SYNC_DIRECTION=both   # pull (default) | push | both
```

## When you need more than config

Config covers the large majority of what a lane needs. Two things it deliberately does NOT cover,
because they need a live Python object, not data:

- **A custom `ContextAssembler`** — e.g. one that layers a rep's own learned preferences on top of
  the default `FileContextStore`, or pushes newly-learned context back to your own backend after a
  run. This is exactly what the `ContextAssembler` adapter role is *for* (see
  [adapters.md](adapters.md)); write one and set `RunnerConfig.context_assembler` in code.
- **A credentialed `MCPServerSpec`** — e.g. one whose `token_provider` derives a live OAuth token
  from a service-account key file. `mcp_servers` holds real objects (a callable token provider
  among them), so it is one of the fields `RunnerConfig.from_file` refuses by name rather than
  silently mis-typing; wire it in code alongside your other adapters.

Both are still additive: build the rest of your config exactly as above, and set these one or two
fields in code before calling `run_lane`.

## Next

- The full field-by-field reference → [Writing a consumer](writing-a-consumer.md)
- Persona resolution in depth → [Personas](personas.md)
- Ship it under cron or systemd → [Deployment](deployment.md)
