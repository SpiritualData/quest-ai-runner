# Guidance cards: rules that are retrieved, not pasted

**Code:** `core/guidance_provider.py` (selection), `adapters/guidance_card_manager.py` (files),
`adapters/quest_guidance_loader.py` (hosted cards), `adapters/feedback_processor.py` (learning),
`core/adapters.py` (the `GuidanceProvider` interface). **Wiring:** `RunnerConfig.guidance_provider`.

A guidance card is one unit of standing instruction: a preference, a policy, a house standard, a
"when you do X, always Y". Cards live as markdown files and/or rows in a host database, and the
orchestrator selects the few that apply to the message in front of it, injecting them as an
`APPLICABLE GUIDANCE` block before it plans.

## The problem this solves

The usual way to steer an agent is one always-on rules file. That file has to grow to cover every
kind of work, so every run pays for every rule, and rules for research work quietly degrade a
refactor. Splitting it into several files per project type trades one problem for another: now
there are N files to keep in sync, they drift, and someone has to remember to switch.

Guidance cards invert it. Write many small cards, each tagged with when it applies, and let
retrieval decide. Nothing is switched by hand, a card that applies everywhere is written once, and
a run's prompt carries the three or so rules that actually bear on it.

Two properties follow that a folder of rules files cannot give you on its own:

- **One rules base, many machines.** Cards can be served from a host database, so every runner on
  every machine reads the same set with no file sync.
- **Rules that improve at the moment of correction.** A human correction can be turned into a card
  automatically, rather than waiting for someone to go edit a file later.

## The card format

A card is a markdown file with optional YAML frontmatter, in the configured cards directory:

```markdown
---
id: python_test_discipline
description: When writing or changing Python tests in any repo
tags: [scope:global, operation:write, task:deep]
---
# Python test discipline

Every behavior change ships with a test that fails without it. Tests run offline: no network, no
API key, no fixture that reaches a real service.
```

| Field | Source | Meaning |
| --- | --- | --- |
| `id` | frontmatter, else the filename stem | Stable identifier. The planner reads a card by this id. |
| `title` | frontmatter, else the first `#` heading, else the id | Short human-facing name. |
| `description` | frontmatter | Becomes the card's `relevance`: the one-line "when does this apply?" used for retrieval and shown in the catalog. |
| `tags` | frontmatter | Scope and matching tags (below). |
| body | everything after the frontmatter | The instruction text. The brain never interprets it, it just renders it. |

Every card is also fingerprinted (SHA-256 of file contents) and stamped with its mtime, so edits,
additions and deletions are detected and hot-reloaded with no restart.

### Where the cards directory is

`GuidanceCardManager` resolves it in this order, first existing directory wins:

1. an explicit `cards_dir` passed to `UniversalGuidanceProvider`
2. `./.quest-guidance` (relative to the process working directory)
3. `/app/prompts/guidance`
4. `~/.quest/guidance`

If none exist, `./.quest-guidance` is used and created on first save. There is currently no
environment variable for this: to pin the directory, construct the provider yourself (see
[Wiring](#wiring) below).

## The tag vocabulary

Tags are how a card says when it applies. They are plain strings, so you can invent your own and
pass them at selection time, but these are the ones the selector scores natively:

| Tag | Effect |
| --- | --- |
| `scope:rep:<id>` | Applies to one AI rep or persona. Most specific. |
| `scope:team:<id>` | Applies to one team. |
| `scope:org:<id>` | Applies to one organization. |
| `scope:global` | Applies everywhere. |
| (no scope tag) | Treated as implicitly global, scored just below an explicit `scope:global`. |
| `operation:<op>` | Applies to a kind of operation: `plan`, `answer`, `read`, `query`, `write`, `update`, `create`, `delete`, `deep`. Operations also match their family, so a card tagged `operation:mutation` applies to any write. |
| `function:<name>` | Applies when a specific function is being called. |
| `task:<type>` | Applies to a task type (`plan`, `answer`, `deep`, `confirm`, ...). |
| anything else | Matched only when the caller passes that tag to `select()`. |

Scope is a hierarchy, not a filter: a rep-scoped card outranks a team-scoped one, which outranks
org, which outranks global. More specific guidance wins where it exists, and general guidance still
applies where it does not.

## How selection works

Once per turn, before planning, the orchestrator asks the provider for the top cards for this
message. `UniversalGuidanceProvider.select()` scores every card:

| Factor | Weight |
| --- | --- |
| Scope match (rep / team / org / global / implicit) | 200 / 150 / 100 / 50 / 40 |
| `operation:` exact match | 120 |
| `function:` match | 110 |
| Operation family match | 60 |
| `task:` match | 80 |
| Custom tag match | 40 each |
| Semantic similarity to the message | up to 70, only when a vector store is wired |
| Keyword overlap with the message | 10 per matching word |

Cards are ordered by scope first, then score, and the top `2 * limit` become candidates. If a model
provider is available, one cheap call (the `balanced` tier) filters those candidates for genuine
relevance to the message, and the survivors are cut to `limit` (`RunnerConfig.guidance_topk`,
default 3). If that call fails, the tag and keyword ranking stands on its own.

The selected cards are rendered into an `APPLICABLE GUIDANCE` block, prepended to the context so
it leads, and a status tick names the cards that were applied. The whole selection is bounded by
`QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS` (default 5.0): a provider that blocks on a slow database
costs the turn its guidance, never the turn itself. Any failure at all leaves the run exactly as if
no guidance were wired.

### Selected guidance is also the quality bar

The cards chosen for a message are not only instructions to the planner. They are carried into goal
verification as the standard the finished work is judged against, so "the rules for this kind of
work" and "what done means for this task" are the same text rather than two things that drift.

### The planner can go looking

Pre-selection is a head start, not the only channel. The planner can also emit:

- `{"list_guidance": true}` for the catalog: every card's id, title and relevance, with bodies
  omitted so the listing stays cheap.
- `{"read_guidance": "<id>"}` for one card's full body.

A card already pre-selected this turn returns a short de-dupe note instead of its body, so the same
instructions are never paid for twice.

### A card can pin a model

If an applicable card's body declares a model preference (`model: <name>` or
`preferred model: <name>`), the deep worker honors it for that work. This is how "research tasks run
on the strongest model, mechanical edits run on the cheap one" becomes a rule you write once rather
than a per-task decision.

## Two sources, merged

`UniversalGuidanceProvider` reads from two places and treats the results identically:

1. **Files**, via `GuidanceCardManager`: markdown in the cards directory. These are plain files, so
   git gives you version history, review, branching and distribution for free.
2. **A dynamic loader**, any callable returning card dicts. It is called on each reload, so new
   cards appear without a restart.

`QuestGuidanceLoader` is the reference dynamic loader: it pulls cards from a Quest backend
(`GET /api/teams/{team_id}/guidance-cards`) at every applicable scope. `build_orchestrator` wires it
automatically when `quest_base_url` and `quest_api_key` are configured, and tolerates a backend that
does not implement the endpoint (a 404 is logged and skipped). That is the shared-rules-base story:
point any number of machines at the same team and they all steer the same way, with the cards
editable in a UI by people who will never touch the repo.

## Learning from corrections

`FeedbackProcessor` turns a human correction into a card:

1. Extract the principle from the feedback ("don't do X", "always do Y").
2. Decide whether it is rep-specific or environment-wide, and tag it accordingly.
3. Match it against existing cards by similarity.
4. Update the matching card, or create a new one.
5. Save through `GuidanceCardManager`, which every consumer then picks up.

`process_rep_correction()` is the same path for a correction aimed at one rep. The point is that
feedback becomes standing guidance rather than a note that only helps the conversation it was said
in.

## Authoring rules

The same discipline that makes a corpus playbook load-bearing makes a guidance card load-bearing.
See [Corpus playbooks](corpus-playbooks.md) for the long form. In short:

- **One rule per card**, short. Cards are selected in threes, so a card that bundles five unrelated
  policies wastes four fifths of its slot.
- **Write `description` as a retrieval hint,** not a summary. It is the "applies when" line the
  selector and the catalog both read. "When writing or changing Python tests" beats "Testing".
- **Say when it does not apply.** A card loaded for the wrong work is worse than no card.
- **Tag the narrowest true scope.** Everything untagged competes for every slot.
- **State the rule, not the reasoning.** One line of why, at most, and only where the rule looks
  arbitrary without it.
- **Prune.** Every card in the catalog costs a little at selection time, and every card in the
  prompt costs a lot.

## Wiring

Guidance is on by default. `build_orchestrator()` constructs a `UniversalGuidanceProvider` when
`cfg.guidance_provider` is `None`, resolving the cards directory as described above and attaching
the Quest loader if Quest is configured. Drop markdown into `./.quest-guidance` and it is live.

To control it, supply your own:

```python
from quest_ai_runner.config import RunnerConfig, build_orchestrator
from quest_ai_runner.core.guidance_provider import UniversalGuidanceProvider

cfg = RunnerConfig(
    retrieval=...,
    model_provider=...,
    guidance_provider=UniversalGuidanceProvider(
        cards_dir="/srv/rules/guidance",          # pin the directory
        dynamic_guidance_loader=my_loader,        # any callable returning card dicts
        vector_store=my_vector_store,             # enables the semantic factor
    ),
    guidance_topk=3,
)
orch = build_orchestrator(cfg)
```

Or implement `GuidanceProvider` (`core/adapters.py`) yourself. Three methods, none of which may
ever raise: `list()` (catalog, bodies empty), `read(card_id)` (one card with body), and `select()`
(top cards for a message, may return `[]`).

> **Implementing `select()`:** the orchestrator calls it as
> `select(user_message, team_id=..., org_id=..., limit=...)`. The `GuidanceProviderBase` ABC
> declares the older `select(user_message, *, k=3, meta=None)` shape, so a custom subclass that
> matches the ABC signature exactly will raise `TypeError` on that call, which is caught and logged
> and leaves the turn with no guidance. Until the two are reconciled, accept `**kwargs` in your
> `select()`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS` | `5.0` | Wall-clock budget for the per-turn `select()` call. Read fresh each call, so it can be tuned without a restart. |

`guidance_topk` (default 3) is a `RunnerConfig` field rather than an environment variable.

## What this does not do yet

Named honestly, because the gaps are small and the shape suggests otherwise:

- **No model-scoped selection.** Different model families need different steering, and there is no
  `scope:model:` factor: the resolved model is not fed into selection. A card can pin a model (see
  above), but the reverse, a rule set that applies only when running on a given model family, has
  to be done today by tagging cards yourself and passing the tag to `select()`.
- **No per-card history in the hosted lane.** File cards get git. Cards served from a host database
  have a current state and no diff or history view.
- **Fingerprints, not versions.** Change detection knows that a card changed, not what it was
  before.
