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
        # OPTIONAL. Leaving deep_runner out entirely gets you this same runner, built from
        # QAR_DEEP_WORKING_DIR/corpus_root + QAR_CLAUDE_PATH. Set it only to change something,
        # e.g. the context preamble below. See "Deep execution is on by default".
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
| `deep_runner` | a `DeepRunner` — runs deep, goal-driven work. **On by default**: leave it unset and one is built for you (see below); pass `None` to turn execution off |
| `escalation` | an `EscalationSink` — where confirm/decision requests go (defaults from the Quest client) |
| `corpus_root` | the org's files/skills root (generic, optional) |
| `rep_sync_resolver` | OPT-IN: map a task to `(user_id, skill_dir)` to run it AS that AI rep (off by default) |
| `rep_sync_direction` | `"pull"` (default) / `"push"` / `"both"`: controls Quest <-> skill-file sync for reps |
| `default_assignee_user_id` | who human-only decisions route to by default |
| `orchestrator`, `poll_interval_seconds`, `poll_lookahead_minutes`, `max_concurrent_tasks` | tuning |
| `extra` | a free-form dict for your own needs |

## Deep execution is on by default

`deep_runner` is tri-state, exactly like `context_assembler`. Doing nothing gives you a working
executor, because a consumer that simply did not know it had to wire one should not silently lose
every request for real work:

| What you pass | What you get |
|---|---|
| **nothing** (leave the field out) | a `SubprocessGoalRunner` is built for you, pointed at `claude` on PATH. If Claude Code isn't installed, you get a loud warning and no runner (never a broken one, never an exception) |
| an **instance** | exactly that runner: `AcpDeepRunner`, your own queue worker, a test double |
| **`None`** | deep execution off, deliberately and silently |

The auto-built runner reads the same environment every other deep-runner knob in this repo reads,
so there is one set of switches and not two:

| Env var | Effect on the default runner |
|---|---|
| `QAR_DEEP_WORKING_DIR` | the subprocess cwd. Falls back to `corpus_root`, then the process cwd |
| `QAR_CLAUDE_PATH` | the worker binary (default `claude`, looked up on PATH) |
| `QAR_DEEP_TIMEOUT_SECONDS` | wall-clock cap per deep run (default 1 hour) |
| `QAR_DEEP_MODELS` | the goal loop's escalation ladder (lives on `OrchestratorConfig`) |

Resolution happens in `config.resolve_deep_runner`, which `build_orchestrator` calls and then
**writes back onto your config**: after `build_orchestrator(cfg)`, `cfg.deep_runner` is always a
real runner or a real `None`, so consumer code can test it directly.

Pass an instance when you need to change something about the worker (a `context_preamble`, tool
gating, a non-default working dir, a different agent entirely). Pass `None` when your deployment
genuinely must not execute, e.g. a read-only chat surface.

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

## Bring your own LLM

`ModelProvider` is a small, three-method `Protocol`:

```python
class ModelProvider(Protocol):
    def plan(self, prompt: str, *, model: str, tool_schema: dict) -> dict: ...
    def answer(self, messages: list[dict], *, model: str, system: str | None = None) -> str: ...
    def list_models(self) -> list[str]: ...
```

Your provider can wrap any SDK. Because the library's `core` never imports a provider directly,
you can add your own logging, analytics, or cost-triage inside the adapter without touching the
brain. **The core ships with zero non-stdlib dependencies and never imports your LLM wrapper.**
The built-in providers live in `quest_ai_runner/adapters/`, not in `core/`: `AnthropicProvider`
(SDK-backed, installed via the optional `[anthropic]` extra) and `ClaudeCliProvider` (drives the
local `claude` CLI headless and keyless, no extra dependency).

A minimal wrapper example:

```python
import your_llm_sdk as sdk

class MyProvider:
    def plan(self, prompt, *, model, tool_schema):
        # Force the model to emit JSON matching tool_schema, then return the raw dict.
        return sdk.complete(prompt, tools=[tool_schema], model=model)

    def answer(self, messages, *, model, system=None):
        return sdk.chat(messages, model=model, system=system)

    def list_models(self) -> list[str]:
        # Return a latest-first list; ModelRegistry buckets by "haiku"/"sonnet"/"opus" substring.
        return sdk.list_models()
```

Pass it in via `RunnerConfig(model_provider=MyProvider())`.

### ModelRegistry fallback: custom or internal model ids

`ModelRegistry` maps tier names (`haiku`, `sonnet`, `opus`) to concrete model ids. It does this
by calling `provider.list_models()` and finding the first id whose lowercase name contains the
tier substring. The bucketed result is cached; re-bucketing happens only when the list changes.

When `list_models()` returns an empty list (e.g. a keyless provider, an unreachable endpoint, or
a provider that has no list API), the registry falls back to `DEFAULT_FALLBACK_TOP`:

```python
DEFAULT_FALLBACK_TOP = {
    "opus":   "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5",
}
```

To override the fallback map for custom or internal model ids, pass `fallback=` at construction:

```python
from quest_ai_runner.core.model_registry import ModelRegistry

registry = ModelRegistry(
    provider=my_provider,
    fallback={
        "opus":   "my-org/large-model-v2",
        "sonnet": "my-org/medium-model-v2",
        "haiku":  "my-org/small-model-v2",
    },
)
```

Or construct the `Orchestrator` directly with a custom registry:

```python
from quest_ai_runner.core.orchestrator import Orchestrator

orch = Orchestrator(retrieval=..., provider=my_provider, registry=registry)
```

If your provider's `list_models()` returns ids that contain `"haiku"`, `"sonnet"`, or `"opus"`
as substrings, the registry auto-buckets them and the fallback is never used. The fallback is only
the last-resort when the live list is empty or unreachable.

### Per-run model override (`model_hint`)

By default the planner picks a tier per step. To override that for one run, pass `model_hint` to
`Orchestrator.run()` or `run_stream()`:

```python
res = orch.run("summarize the quarter", model_hint="opus")
```

The hint is an opaque string resolved by your `ModelRegistry` exactly like a planner-chosen tier,
with precedence `model_hint` > `plan.model_tier` > the step's default tier. It applies to answer
and deep steps only; the planner's own cheap structured calls stay on the configured planner tier.
A value the registry does not understand degrades to the registry default ("sonnet") rather than
raising. Note that the default registry resolves only the tier names (`haiku`/`sonnet`/`opus`); if
you want hints to carry raw model ids, supply a registry whose `resolve_tier` understands them.

The runner wires this up for you: `TaskExecutor` forwards a task document's optional `"model"`
field as the `model_hint`, so a per-task model override stored on the task is honored at execution
time with no extra code in your poller.

## AI reps: run a task AS a team member (opt-in)

A team's AI reps each have a Quest profile (a persona plus learned corrections) and a local Claude
skill file. The runner can execute a task AS the right rep, with NO extra glue beyond one resolver.

Turn it on by supplying `rep_sync_resolver` on your `RunnerConfig`: given a task dict, return the
`(user_id, skill_dir)` of the rep that should run it, or `None` to skip. That is the only required
wiring. With it set, the DEFAULT does the complete, correct thing:

1. The poller resolves the rep for the task.
2. It PULLS the rep's Quest profile into the local skill file (Quest is the source of truth at
   execution time), preserving any human-authored content outside the managed sections.
3. It builds a per-run preamble from that file's persona and learned corrections (combined with the
   runner's context doctrine) and injects it into the deep run, so the run behaves AS that rep.

```python
cfg = RunnerConfig(
    ...,
    rep_sync_resolver=lambda task: (task["assignee_user_id"], f"/skills/{task['assignee_slug']}"),
    # rep_sync_direction defaults to "pull"
)
```

`rep_sync_direction` controls the Quest <-> skill-file sync:

- `"pull"` (default): pull Quest into the skill file before the run. No push-back. Use this when
  Quest is the source of truth and you just want the rep to act as its current self.
- `"push"`: push the local skill file UP to Quest AFTER the run only (no pre-run pull, so no persona
  injection). Use this when the local file is the source of truth.
- `"both"`: pull first (the run acts current), then push back after the run.

Both the pre-run pull and the post-run push are best-effort: a sync failure is logged and never
fails the task. This whole capability is OFF unless `rep_sync_resolver` is set, so a consumer that
does not wire it sees exactly the prior behaviour. It does NOT require a `ContextAssembler`.

Under the hood the executor accepts `execute(task, *, rep_preamble=...)` and threads the preamble
into `Orchestrator.run(rep_preamble=...)`, which forwards it to the deep run only for a `DeepRunner`
whose `run_goal` accepts a `context_preamble` kwarg (the reference `SubprocessGoalRunner` does).
Older deep runners are left untouched.

### A task can carry its own persona (`rep_preamble` on the task document)

You do not need reps at all to control the voice of one task. When a task document has a
`"rep_preamble"` field (a non-empty string), the poller uses it as that task's persona: it is the
deep run's `context_preamble` and the voice of the fold-back "done" report. A rep resolved by your
`rep_sync_resolver` still wins; the task field is the fallback for tasks that have no rep of their
own. A value that is not a non-empty string is ignored.

This exists for work deferred out of a live conversation. The side that queues the task already
knows the persona/system prompt that conversation is running with, so it stamps that string on the
task; when the task finishes and its report is posted back into the same conversation, the report
speaks in the same voice as the replies already there instead of a generic one. No resolver, no
profile, no extra wiring in your poller.

## Deep runners that emit a STRICT format (code, JSON, a patch)

When the async card updater is on, the orchestrator asks every deep worker for FUTURE CONTEXT: a few
bullets naming what would help a similar future request (collections and their ids, key files, schema
it learned, stable facts). Those bullets are what the updater learns from, so they are how your cards
find out which context was actually used and useful. Every runner is asked, including a code
generator, which knows the most reusable facts of all.

What differs per runner is the **channel**, declared on the runner:

```python
from quest_ai_runner.core import DeepResult, FUTURE_CONTEXT_VIA_FIELD

class MyCodeRunner:
    # My output is Python. Anything appended to it is a syntax error, so send the bullets
    # out of band instead of asking the worker to end its output with a prose section.
    future_context_channel = FUTURE_CONTEXT_VIA_FIELD

    def run_goal(self, *, goal, brief, model=None, max_turns=None):
        code, bullets = my_worker(brief)      # e.g. two fields of one tool-call result
        return DeepResult(met=True, output=code, future_context=bullets)
```

| `future_context_channel` | who declares it | what the brief asks for | where the orchestrator reads it |
|---|---|---|---|
| `FUTURE_CONTEXT_VIA_OUTPUT` (**default**) | prose runners, incl. the reference `SubprocessGoalRunner` | end your output with the delimited `=== FUTURE CONTEXT ... ===` section | parsed out of `output`, which is then stripped |
| `FUTURE_CONTEXT_VIA_FIELD` | a runner whose output is a strict format | return the bullets in your result's future-context field, never inside the output | `DeepResult.future_context` |

The attribute is read with `getattr`, so a runner that never declares it is a prose runner and
behaves exactly as before. Both channels land in the same place: `DeepResult.future_context`,
normalized the moment the runner returns, and read from there by the card updater and by the
"what I'll remember" panel.

**You never have to strip anything.** The orchestrator cuts the delimited section from
`DeepResult.output` at that same seam, whatever the runner declared, so a worker that ignores the
instruction and appends the block to generated code still hands you a payload that parses. Do not
work around the instruction by removing it from the brief: that keeps the payload clean but stops
your deep runs from teaching the cards anything, which is the expensive half of the feature.

## Shutting down background indexing

`FileContextStore` builds and refreshes its index on a background thread so chat is usable
immediately. That thread is **owned**, not fire-and-forget: it walks the corpus and runs
`git hash-object` per file, so it must not outlive whatever started it.

- `FileContextStore.close()` stops that store's indexing at its next checkpoint and guarantees no
  further `git` subprocess is spawned. Cards already written are kept (indexing is incremental and
  resumes on the next start).
- `config.shutdown_background_index(timeout=...)` closes every store an index thread was started for
  and joins those threads. Call it whenever an orchestrator's owner goes away: a rebuild of your
  wiring, a tenant shutting down, a CLI about to exit, a test finishing.

## Next

- Plug in a non-file source or a different model → [Implementing adapters](adapters.md)
- Run it as a service → [Deployment](deployment.md)
