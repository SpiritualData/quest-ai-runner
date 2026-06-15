# How to implement adapters

The brain depends only on **four interfaces**, defined in `quest_ai_runner.core.adapters`. A
consumer supplies concrete implementations via `RunnerConfig`. They are `typing.Protocol`s, so you
can satisfy them structurally (just match the methods) or subclass the provided ABCs
(`RetrievalAdapterBase`, etc.).

| Interface | Role | Reference impl |
|---|---|---|
| `RetrievalAdapter` | GATHER — read/grep/query your source of truth | `FilesAdapter`, `CachedDbAdapter` |
| `ModelProvider` | the LLM — plan / answer / list models | `AnthropicProvider` |
| `DeepRunner` | run a bounded, goal-driven autonomous task | `SubprocessGoalRunner` |
| `EscalationSink` | raise a human-only confirm/decision | `QuestDecisionSink` |

## RetrievalAdapter

How the brain gathers grounding. All three methods return an `Observation`.

```python
from quest_ai_runner.core.adapters import Observation

class MyRetrieval:
    def read_section(self, rel_path, *, start_line=None, end_line=None,
                     heading=None, max_bytes=None) -> Observation:
        ...  # Observation(kind="read", rel_path=..., text=...) or kind="error"

    def grep(self, pattern, *, scope=None, max_hits=None) -> Observation:
        ...  # Observation(kind="grep", pattern=..., hits=[{rel_path, line_no, line}, ...])

    def query(self, spec) -> Observation:
        ...  # structured query (e.g. a DB lookup); return kind="error" if unsupported
```

The brain's loop calls these; it never opens a file or socket itself. The reference `FilesAdapter`
is read-only and hard-scoped inside a root, skipping secret-ish/binary/oversize files.
`CachedDbAdapter` wraps a `query` callable (e.g. a Mongo `find`) with a short TTL so the brain
grounds on **live** data without syncing it to files.

## ModelProvider

The LLM behind the brain.

```python
class MyProvider:
    def plan(self, prompt, *, model, tool_schema) -> dict:
        # ONE cheap structured decision: {"action": "read|answer|deep|confirm", "model_tier": ..., ...}
        ...
    def answer(self, messages, *, model, system=None) -> str:
        ...
    def list_models(self) -> list[str]:
        # live model ids; the ModelRegistry buckets these into tiers (haiku/sonnet/opus, etc.)
        ...
```

The reference `AnthropicProvider` wraps the Anthropic SDK and is installed via the `[anthropic]`
extra. Swap in any provider (OpenAI, a local model, a deterministic stub for tests) by matching this
shape — see the stub providers in `tests/conftest.py` and `examples/e2e_demo.py`.

## DeepRunner

Runs deep, goal-driven work to a checkable done-standard.

```python
from quest_ai_runner.core.adapters import DeepResult

class MyDeepRunner:
    def run_goal(self, *, goal, brief, model=None, max_turns=None) -> DeepResult:
        # do the work bounded by max_turns; return whether the goal was MET
        return DeepResult(met=True, output="...summary...")        # or met=False, error="..."
```

The reference `SubprocessGoalRunner` spawns Claude Code headless with `/goal <goal> --max-turns N`;
exit code 0 = goal met, non-zero = limit/error. Working dir, binary, model, context preamble, and
tool gating are all config (`SubprocessConfig`). Plug in a different agent by implementing this one
method.

**Escalating from inside a deep run.** A spawned worker can itself hit a human-only step mid-run
(an unapproved outward send, an irreversible commitment). If the consumer's context preamble gives
the worker an escalation mechanism (e.g. "create a decision-request via X"), the worker reports the
raised decision back to the runner by printing, on its own line, `QAR-ESCALATED: <decision_id>`
(the `ESCALATION_MARKER` contract in `core/goal_runner.py`). `SubprocessGoalRunner` parses the
marker and returns `DeepResult(met=False, decision_id=...)` regardless of exit code, so the
executor reports the task as `needs_you` with the decision linked — the ask shows up in the
consumer's UI attached to the paused task instead of the task closing as done. A custom
`DeepRunner` can set `DeepResult.decision_id` directly; `GoalRunner` normalizes `met=True` +
`decision_id` to not-met so a paused run never reports done.

## EscalationSink

Where the brain raises a human-only step.

```python
class MyEscalation:
    def escalate(self, escalation) -> str:
        # create a decision/approval request somewhere; return its id (a string)
        return "decision_123"
```

The reference `QuestDecisionSink` (in `runner/quest_client.py`) raises a Quest team decision-request
with `default_on_silence="hold"` and returns the `decision_id`, which the executor stamps onto the
task as `needs_you`.

## GuidanceProvider (optional)

A sixth, OPTIONAL role: retrievable **use-case-specific instructions**. It lets a host app shrink
its ALWAYS-ON core prompt to only what applies to *every* input, moving everything else (product
facts, feature-flow guides, behavior policies) into a corpus of opaque **guidance cards** the brain
retrieves on demand. Cards are opaque text to the runner — it stays app-agnostic.

```python
from quest_ai_runner.core.adapters import GuidanceCard, GuidanceProviderBase

class MyGuidance(GuidanceProviderBase):
    def list(self):                      # cheap catalog: id + title + relevance, body EMPTY
        return [GuidanceCard(id="quest_creation", title="Creating a quest",
                             relevance="the user wants to start a new quest")]

    def read(self, card_id):             # one card WITH body, or None if unknown
        ...

    def select(self, user_message, *, k=3, meta=None):  # optional semantic pre-selection; may be []
        ...
```

When wired (`RunnerConfig.guidance_provider=...`, or `Orchestrator(guidance=...)`), the orchestrator
calls `select()` ONCE before planning and prepends the chosen cards as an `--- APPLICABLE GUIDANCE
---` block to the context. The planner also gains two discovery verbs, `list_guidance` and
`read_guidance` (id), that flow through the same observation path as a read; a `read_guidance` of a
card already pre-selected this turn returns a short de-dupe note. All three methods must NEVER raise.
Leave `guidance_provider` unset for exactly today's behavior (no guidance). `OrchestratorConfig.guidance_topk`
(default 3) tunes how many cards are pre-selected.

## Wiring them up

```python
cfg = RunnerConfig(
    retrieval=MyRetrieval(),
    model_provider=MyProvider(),
    deep_runner=MyDeepRunner(),
    escalation=MyEscalation(),     # optional; defaults to QuestDecisionSink in the runner
    quest_base_url=..., quest_api_key=..., team_id=...,
)
```

See [writing-a-consumer.md](writing-a-consumer.md) for the full config and
[architecture.md](architecture.md) for how the brain calls these in its loop.
