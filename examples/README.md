# Examples

These are runnable references for wiring `quest-ai-runner` to your own Quest backend. The library
itself is domain-free — it bakes in **no** org, key, team, corpus, or persona. A *consumer*
supplies those, and these examples show how. **None of these files contain real keys, ids, emails,
or paths** — every specific is read from the environment. Copy `.env.example` (at the repo root) to
`.env`, fill it in, and load it before running.

| File | What it shows |
|---|---|
| `minimal_lane.py` + `qar.toml` | The SMALLEST real lane: a TOML config file layered under the environment, and `runner.lane.run_lane` as the entire entry point. Start here — see [`docs/tutorial-your-first-lane.md`](../docs/tutorial-your-first-lane.md) for the walkthrough (a persona roster, a corpus, a deep-run preamble, a folder map, each added as a few lines of config). |
| `custom_consumer.py` | Build a `RunnerConfig` for one lane entirely from environment variables — the adapter wiring (`FilesAdapter` + `AnthropicProvider` + `SubprocessGoalRunner`), the deep-run context preamble, and decision routing. Copy and adapt this for your team. |
| `run_lane.py` | Run that lane's `Poller` — `--check` (validate key), `--once` (cron), or loop forever (service). **Predates `runner.lane.run_lane`** (this repo's own shared driver, moved in from what used to be an external, hand-maintained copy) and is kept for reference alongside `custom_consumer.py`'s from-scratch adapter wiring; `minimal_lane.py` above is the recommended starting point for a new lane's entry point. |
| `e2e_demo.py` | End-to-end smoke test against a **live** Quest backend: enqueue → discover → claim → run → report → re-read. Only the LLM is stubbed (no `ANTHROPIC_API_KEY` needed), so every other code path is the real one. |

## Quick start

```bash
cp .env.example .env          # then edit .env with your QUEST_* and ANTHROPIC_API_KEY
set -a && . ./.env && set +a  # load it into the environment

python examples/minimal_lane.py --check   # validate your key + identity
python examples/minimal_lane.py --once    # run one scan
```

Prefer zero code? The installed console entry point does the same thing from env alone (optionally
layered under a TOML file via `--config`/`QAR_CONFIG_FILE` — see
[`docs/tutorial-your-first-lane.md`](../docs/tutorial-your-first-lane.md)):

```bash
quest-ai-runner --check
quest-ai-runner --once
```
