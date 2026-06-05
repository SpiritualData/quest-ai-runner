# How to write a consumer

A *consumer* is the thin layer that supplies your specifics — Quest connection, adapters, corpus,
persona, decision routing — to the generic library. Everything flows through a single
`RunnerConfig`. The library hardcodes nothing.

> Keep your real keys and ids in the environment, never in code. The shipped
> [`examples/`](../examples/) contain only env lookups and placeholders — copy them.

## The shape

```python
from quest_ai_runner.adapters import AnthropicProvider, FilesAdapter
from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

def build_config() -> RunnerConfig:
    corpus = os.getenv("QAR_CORPUS_ROOT")
    return RunnerConfig(
        # --- Quest connection ---
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),   # qsk_...  (executor identity)
        team_id=os.getenv("QUEST_TEAM_ID", ""),
        runner_label="my-team-runner",                  # shows on the env heartbeat

        # --- adapters (you choose which) ---
        retrieval=FilesAdapter(corpus) if corpus else None,
        model_provider=AnthropicProvider(),
        deep_runner=SubprocessGoalRunner(SubprocessConfig(
            working_dir=corpus,
            context_preamble="You are executing a task for <your team>. Ground on the corpus; "
                             "surface any human-only step as a decision-request.",
        )),
        corpus_root=corpus,

        # --- routing ---
        default_assignee_user_id=os.getenv("QAR_DECISION_ASSIGNEE"),
    )
```

## `RunnerConfig` fields

| Field | Purpose |
|---|---|
| `quest_base_url`, `quest_api_key`, `team_id` | the Quest connection; the key is the executor identity |
| `runner_label` | human-readable tag sent on the team-environment heartbeat |
| `retrieval` | a `RetrievalAdapter` — how the brain gathers grounding |
| `model_provider` | a `ModelProvider` — the LLM (plan/answer/list_models) |
| `deep_runner` | a `DeepRunner` — runs deep, goal-driven work |
| `escalation` | an `EscalationSink` — where confirm/decision requests go (defaults from the Quest client) |
| `corpus_root` | the org's files/skills root (generic, optional) |
| `default_assignee_user_id` | who human-only decisions route to by default |
| `orchestrator`, `poll_interval_seconds`, `poll_lookahead_minutes`, `max_concurrent_tasks` | tuning |
| `extra` | a free-form dict for your own needs |

## Validate before you run

```python
cfg = build_config()
problems = cfg.validate()     # [] means ok
if problems:
    for p in problems:
        print("config problem:", p)
```

`validate()` requires `quest_base_url`, `quest_api_key`, a `retrieval` adapter, and a
`model_provider`. The CLI and the example runner call this and **degrade visibly** (log + exit 0)
when a key isn't provisioned yet, so cron/systemd don't error-spam.

## Build and run

```python
from quest_ai_runner.config import build_orchestrator   # in-process brain
from quest_ai_runner.runner.poller import Poller         # executor lane

orch = build_orchestrator(cfg)        # for in-process chat: orch.run(message)
Poller(cfg, state_path="qar_state.json").run_forever()   # for the executor lane
```

## Capabilities are derived, not asserted

`config.derive_capabilities(cfg)` reports `{web, corpus, code}` straight off your wired adapters —
`corpus` when a file/corpus retrieval adapter is set, `code` when a `DeepRunner` is set, and `web`
when the deep-runner can actually browse (the reference `SubprocessGoalRunner` derives this from its
tool gating via `web_enabled()`). The runner sends this on its heartbeat so Quest's router only
sends work the lane can honestly do. Don't claim a capability you haven't wired.

## Next

- Plug in a non-file source or a different model → [Implementing adapters](adapters.md)
- Run it as a service → [Deployment](deployment.md)
