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
| `FileWriter` (optional) | write a file inside a confined root, with backups | `FilesWriter` |
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

## Notion and Google Chat (opt-in, read-only)

Two first-class context channels, both generic, both off unless configured, both read-only.

| | `NotionAdapter` | `GoogleChatAdapter` |
|---|---|---|
| module | `adapters/notion_adapter.py` | `adapters/google_chat_adapter.py` |
| reads | configured databases (`database_ids`: alias to id): rows, pages, block text | configured spaces (`space_names`): threads and messages |
| auth | injected `token_provider` (`env_token_provider("NAME")`, `file_token_provider(path)`, `static_token_provider(t)`) | injected; `service_account_token_provider` for a Workspace |
| HTTP | stdlib `urllib`, `Notion-Version` pinned (`NOTION_VERSION`) | stdlib `urllib` |
| never raises | yes: `Observation(kind="error")` | yes |
| reference type | `notion_page` | `chat_thread` |
| context source | `notion_database` | `google_chat` |

**Retrieval surface (Notion).** `list_sources` (the configured databases by alias), `describe_source`
(a database's properties and types), `query({"database": "tasks", "filter": {"Status": "In
progress"}, "query": "terms", "limit": 10})`, `read_section(<page id | link | alias/id>)` (properties
plus block text, bounded by `max_blocks` and `max_page_chars`), and `grep(pattern, scope=<alias>)`
across the rows. A simple filter is `{"Property": value}` or `{"Property": {"operator": value}}`,
turned into Notion's own filter shape using the database's real property types; an unknown property
or an operator that does not apply returns an error naming the valid choices. `make_locator` and
`resolve_reference` let a learned `notion_page` reference re-fetch the page fresh, as `chat_thread`
does for Chat, and are discovered automatically from the retrieval stack.

**Read-only is enforced in code, not by convention.** The Notion adapter has one method that sends a
request, and it refuses anything but a `GET` on pages, databases and blocks and the single `POST`
Notion uses to query a database (which reads, and changes nothing). A create, update, archive or
delete cannot be sent. A database that is not configured, and a page whose parent is not a configured
database, are refused. Set the Notion integration to "read content" only as well. Notion-Version is
pinned to `2022-06-28`, the last version that queries `/databases/{id}/query`; later versions split a
database into data sources, so moving the pin is a deliberate change.

**Google Chat fails closed.** Domain-wide delegation can read every space its subject is in, so the
declarative `google_chat` block REQUIRES `space_names` and wires nothing without it. The typed read
the context source uses (`GoogleChatAdapter.fetch_messages_since`) refuses any space not on that list
before making a request. Scopes default to the read-only pair.

Declarative wiring (the same blocks the context-updates doc describes):

```toml
[notion]
token_env = "NOTION_TOKEN"
[notion.database_ids]
tasks = "0123456789abcdef0123456789abcdef"

[google_chat]
service_account_file = "/path/to/chat-sa.json"
subject = "someone@example.org"
space_names = ["spaces/AAAA1111"]
```

A block is built only when its credentials exist (a token in the named environment variable or file;
a service-account file that is on disk), logs why and wires nothing otherwise, and never puts a token
in a log line. See [context-updates.md](context-updates.md) for the `notion_database` and
`google_chat` sources that let an autopilot pass see what changed.

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

**This is the only adapter that is wired for you.** `RunnerConfig.deep_runner` is tri-state:
leave it unset and `config.resolve_deep_runner` builds the `SubprocessGoalRunner` above from
`QAR_DEEP_WORKING_DIR`/`corpus_root` + `QAR_CLAUDE_PATH`; pass an instance to use your own; pass
`None` to disable execution deliberately. If `claude` isn't on PATH the resolution warns loudly and
leaves you with no runner rather than a runner that would fail on every spawn. See
[writing-a-consumer.md](writing-a-consumer.md#deep-execution-is-on-by-default).

A second implementation ships alongside it: **[`AcpDeepRunner`](acp-deep-runner.md)** runs the same
contract over the Agent Client Protocol — a live session with the Claude ACP agent rather than a
one-shot subprocess — which is what makes MID-TURN STEERING possible (a message queued while the
deep turn is running is injected into that turn, not held until the next attempt). It is opt-in and
purely additive: `SubprocessGoalRunner` is unchanged and remains the default, and selecting the
other one is just `RunnerConfig.deep_runner`. It needs the `[acp]` extra and Node >= 22.

A third implementation is the **[`FastEditRunner`](fast-edit-runner.md)**, which does not spawn
anything: it lands a bounded file edit in ONE model call, applied in process through a `FileWriter`
(whole-file rewrite for short files, SEARCH/REPLACE above a line threshold). It is off by default
and appears only when you wire `RunnerConfig.file_writer`, which is also the only way anything in
this library gains write access to your files. When it is wired the deep runner is resolved as an
ORDERED LADDER — `[FastEditRunner, SubprocessGoalRunner]` — indexed by attempt, so a fast edit that
fails the goal loop's existing verification escalates to the full worker with no new control flow.
A consumer that wires no writer gets a one-rung ladder and unchanged behaviour.

**Escalating from inside a deep run.** A spawned worker can itself hit a human-only step mid-run
(an unapproved outward send, an irreversible commitment). If the consumer's context preamble gives
the worker an escalation mechanism (e.g. "create a decision-request via X"), the worker reports the
raised decision back to the runner by printing, on its own line, `QAR-ESCALATED: <decision_id>`
(the `ESCALATION_MARKER` contract in `core/goal_runner.py`; matched anywhere WITHIN a line, not
only at its start, so a worker that leads into it with other text is still caught).
`SubprocessGoalRunner` parses the marker and returns `DeepResult(met=False, decision_id=...)`
regardless of exit code, so the executor reports the task as `needs_you` with the decision linked,
and the ask shows up in the consumer's UI attached to the paused task instead of the task closing
as done. A custom `DeepRunner` can set `DeepResult.decision_id` directly; `GoalRunner` normalizes
`met=True` + `decision_id` to not-met so a paused run never reports done.

If the marker is missing entirely (the worker forgot, or its output got garbled), the run would
otherwise close done with the decision it actually created orphaned. `Orchestrator._run_deep`
recovers it anyway when the wired `EscalationSink` implements the OPTIONAL
`open_decision_ids_for_quest(quest_id)` capability: it snapshots the quest's open decision ids
before each deep attempt and, when the attempt's result carries no `decision_id`, diffs the
snapshot again afterward: a new id is attached to the result exactly as if the marker had
reported it. `QuestDecisionSink` implements this against `list_open_decisions_for_quest`; a sink
implementing only `escalate` (the required surface) is unaffected, the probe is just a no-op.

## EscalationSink

Where the brain raises a human-only step.

```python
class MyEscalation:
    def escalate(self, escalation) -> str:
        # create a decision/approval request somewhere; return its id (a string)
        return "decision_123"

    # OPTIONAL: enables the orphan-decision recovery above. Return the ids of currently OPEN
    # decisions for this quest, or an empty set if you have no way to look this up.
    def open_decision_ids_for_quest(self, quest_id: str) -> frozenset:
        return frozenset()
```

The reference `QuestDecisionSink` (in `runner/quest_client.py`) raises a Quest team decision-request
and returns the `decision_id`, which the executor stamps onto the task as `needs_you`. It never
files a decision with an empty assignee: it tries the `Escalation.assignee` given, then its
configured `default_assignee_user_id` (`QAR_DECISION_ASSIGNEE`), then the quest's own owner, and
raises (logged, no request sent) if none of those resolve. `default_on_silence` defaults to
`"hold"`; an `Escalation.deadline` (or the sink's `QAR_DECISION_DEFAULT_DEADLINE_HOURS` default)
lets a `"proceed"` decision auto-resolve at its deadline instead of blocking forever.

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
[ARCHITECTURE_STANDARDS.md](ARCHITECTURE_STANDARDS.md) for how the brain calls these in its loop.
