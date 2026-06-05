# Examples

These are runnable references for wiring `quest-ai-runner` to your own Quest backend. The library
itself is domain-free — it bakes in **no** org, key, team, corpus, or persona. A *consumer*
supplies those, and these examples show how. **None of these files contain real keys, ids, emails,
or paths** — every specific is read from the environment. Copy `.env.example` (at the repo root) to
`.env`, fill it in, and load it before running.

| File | What it shows |
|---|---|
| `custom_consumer.py` | Build a `RunnerConfig` for one lane entirely from environment variables — the adapter wiring (`FilesAdapter` + `AnthropicProvider` + `SubprocessGoalRunner`), the deep-run context preamble, and decision routing. Copy and adapt this for your team. |
| `run_lane.py` | Run that lane's `Poller` — `--check` (validate key), `--once` (cron), or loop forever (service). Mirrors the `quest-ai-runner` console entry point with explicit wiring you can edit. |
| `e2e_demo.py` | End-to-end smoke test against a **live** Quest backend: enqueue → discover → claim → run → report → re-read. Only the LLM is stubbed (no `ANTHROPIC_API_KEY` needed), so every other code path is the real one. |

## Quick start

```bash
cp .env.example .env          # then edit .env with your QUEST_* and ANTHROPIC_API_KEY
set -a && . ./.env && set +a  # load it into the environment

python examples/run_lane.py --check   # validate your key + identity
python examples/run_lane.py --once    # run one scan
```

Prefer zero code? The installed console entry point does the same thing from env alone:

```bash
quest-ai-runner --check
quest-ai-runner --once
```
