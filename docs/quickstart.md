# Quickstart

This tutorial takes you from install to a running executor lane. It assumes Python 3.10+.

## 1. Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[anthropic,dev]'   # core + runner are stdlib-only; this adds the model provider + pytest
```

Verify the install and run the (offline) test suite:

```bash
python -c "import quest_ai_runner; print(quest_ai_runner.__version__)"
python -m pytest -q
```

## 2. Use the brain in-process (no Quest needed)

The **brain** (`core`) answers a question by grounding on a corpus you point it at. This needs only
a model key (`ANTHROPIC_API_KEY`) and a folder of files.

```python
from quest_ai_runner.config import RunnerConfig, build_orchestrator
from quest_ai_runner.adapters import FilesAdapter, AnthropicProvider

cfg = RunnerConfig(
    retrieval=FilesAdapter("/path/to/docs"),     # any folder of text/markdown/code
    model_provider=AnthropicProvider(),          # reads ANTHROPIC_API_KEY from the env
)
orch = build_orchestrator(cfg)

result = orch.run("Summarize the onboarding doc.")
print(result.kind)   # "answer" | "deep" | "confirm"
print(result.text)
```

The brain runs a bounded plan → gather → re-plan → answer loop: it greps and reads the matching
sections of your corpus, then answers grounded in what it found. See
[ARCHITECTURE_STANDARDS.md](ARCHITECTURE_STANDARDS.md) for the loop, and [streaming-and-modes.md](streaming-and-modes.md)
to stream events as it works.

## 3. Run the executor lane (poll Quest for due tasks)

The **runner** discovers due AI tasks from Quest, claims them, runs them through the brain, and
reports results back — escalating human-only steps as decision-requests.

The installed CLI builds everything from environment variables:

```bash
export QUEST_BASE_URL=https://api.example.org
export QUEST_API_KEY=qsk_...        # your executor identity (keep secret)
export QUEST_TEAM_ID=team_...
# export QUEST_ORG_ID=org_...   # optional: registers the env heartbeat org-wide, see deployment.md
export QAR_CORPUS_ROOT=/path/to/corpus
export ANTHROPIC_API_KEY=sk-ant-...

quest-ai-runner --check    # validate the key + identity
quest-ai-runner --once     # one scan then exit (good for cron)
quest-ai-runner            # loop forever (good for a service)
```

Prefer to see the wiring in code, or drive it from a config file instead of env vars? Copy
[`examples/minimal_lane.py`](../examples/minimal_lane.py) + [`examples/qar.toml`](../examples/qar.toml)
— see [Your first lane](tutorial-your-first-lane.md) for the walkthrough. For a bigger reference with
every adapter wired by hand, see [`examples/custom_consumer.py`](../examples/custom_consumer.py) and
[`examples/run_lane.py`](../examples/run_lane.py) (the latter predates `runner.lane.run_lane` and is
kept for reference, not as the recommended starting point any more). To prove the full round-trip
against a live backend, use [`examples/e2e_demo.py`](../examples/e2e_demo.py) (it stubs only the LLM).

## 4. Where to go next

- Make it yours → [Writing a consumer](writing-a-consumer.md)
- Add a persona roster, a corpus, a deep-run preamble → [Your first lane](tutorial-your-first-lane.md)
- Plug in a different source or model → [Implementing adapters](adapters.md)
- Ship it → [Deployment](deployment.md)
