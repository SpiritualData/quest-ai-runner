# CLAUDE.md: working rules for this repository

This file orients any contributor (human or AI) working in `quest-ai-runner`, and encodes the
**hard rules** that keep this public, open-source repository clean. Read it before making changes.

## What this project is

`quest-ai-runner` is **Quest's connector** and the production consumer of the hub of hubs (`hubofhubs`, a required
dependency; `quest_ai_runner/the_self/assembler/hub.py` imports it at startup and registers QAR's parts under names QAR owns): the
orchestrator **brain** (`the_intelligence/`) plus the queued-task **executor** (`the_consciousness/request_intake/`) for Quest AI
tasks. It is published as open source (Apache-2.0) and is meant to run for *any* org, not one specific deployment. The
brain (`the_intelligence/`) holds the plan, gather, answer loop; the Quest-specific parts are the Body arms (the Quest client, card stores, configuration), Request Intake and the terminal. Generic engines
are moving into the hub seat by seat (`docs/explanation/hub-of-hubs.md`); QAR keeps what knows Quest.
- **The package mirrors the hub's five containers** (`the_self/`, `the_body/`, `the_intelligence/`, `the_world/`, `the_consciousness/`) and holds a
  folder only where QAR overrides or adds a solution; the folder path is the hub's solution path (`the_intelligence/mind/planner/` is QAR's arm of the
  hub's Planner). A solution's records (`solutions.csv`, `implementations.csv`, `metrics.csv`, generated `README.md`) sit in the folder of its code.
  Quest is the interface: the Quest API client and card stores are Body arms, quests and goals are Objectives under Higher Mind, the task queue
  and terminal are Request Intake and Terminal under the Consciousness. There are no layer folders (`core/`, `adapters/`, `runner/` are gone).
- **Interfaces first, one front door.** A solution declares its interface once, in the `interface.py` beside its code (five Protocols still sit in their implementation files because a value type or sibling port moves with them: `reply-in-steps`, `show-the-ais-work`, `describe-an-attachment`, `search-the-web`, `store-feedback`; a Protocol with `@solution(id, entry=...)`; an override of a
  hub solution extends the hub's Protocol and never redeclares it), and its role (purpose, `out_of_scope`, `delegates_to`) in its `solutions.csv` row. `quest_ai_runner.run("<id>", input)` runs any
  solution through the entry its interface declares (beside `hubofhubs.run`); `quest_ai_runner/api.py` is the only surface a host application (the Quest backend) imports: `run` and the few
  names it implements or reads, grouped by solution, never underscore-prefixed, never a capability probe (the interface is the contract). A name is added there only when a consumer outside
  this repository imports it. `python -m quest_ai_runner <cli|tools|lane|requirements>` is the one stable command line (deep workers and lane scripts call it, never a module path).
- **The flow is code.** `quest_ai_runner/the_self/respond.py` is the flow of one request (Request intake, Higher mind, Subconscious, Mind, Nervous System, Critical Thinking); the Mind's
  orchestrator loop is the selected arm and hosts most parts until a hub arm of that part wins by metric. The intent paragraph in `esoi/intent.csv` states the same order.
- **Autopilot is the hub's.** WHEN standing work starts is the hub's Proactivity solution and WHAT matters first is the hub's Prioritization solution (both reached with `quest_ai_runner.run`); QAR
  supplies only the Quest-specific parts, Quest as the source of events (`feed-context-sources`) and of priorities (`hold-the-intents`), and the run-or-skip gate (`gate-an-autopilot-pass`, which
  delegates to both). New autopilot logic goes to those two hub solutions first; see `docs/explanation/hub-of-hubs.md`.
- **The hub only through `hubofhubs.api` and `hubofhubs.run`.** Never import a hub internal path; a missing name is
  added to the hub's `api.py` first.
- The orchestrator loop (`the_intelligence/mind/orchestrator*.py`, split by role into mixins) is the one arm of the hub's Mind that QAR selects today;
  `the_intelligence/mind/delegation/` holds the deep run and its goal runner. Tests are in `tests/`, evaluation components in `evaluation/components/`.
- `config`: `RunnerConfig` and the `config_*` modules are in `the_body/configuration/`, the single place a consumer supplies everything specific.
- `examples/`: runnable reference consumers (env-driven, no real data).
- `docs/`: four Diataxis folders (`tutorials/`, `how-to/`, `explanation/`, `reference/`), indexed by `docs/README.md`; `esoi docs check` enforces them and `esoi docs wiki` generates the wiki from them (never edit the wiki). A new page goes in exactly one folder and in the index.

Module names describe **architectural role**, not domain. "core" is the stable center everything
depends on; "runner" is the executor; "adapters" are the swappable implementations. See
`docs/reference/ARCHITECTURE_STANDARDS.md` for the full architecture and the standards code here must follow.

## Evolvable structure (ESOI): the registry and the check

The repo's structure is data, not folklore, and the checking, running and measuring of it is Joshua's `esoi` package
(a dependency pinned in `pyproject.toml`, a runtime one because implementations are declared with its decorators; `esoi rules`
lists its rules, so they are not restated here, and the steps and the philosophy are the library's docs and the
evolvable-structure skill, not copied here). **The records live beside the code** (`[records] beside_code` in `esoi.toml`; the standard is
`evolvable-structure/docs/reference/records-beside-code.md`): each record is kept once, in the folder that holds the code it describes, and a generated
`README.md` in each of those folders shows its solutions, implementations, metrics and diagram. `esoi/` keeps the top intent (`intent.csv`, why this project
exists, in its own words, with the generated `README.md` that links to every folder), `components.csv` (placement only), `waivers.csv`, `overrides.csv`,
`tags.csv`, `hub_seats.csv`, `env_descriptions.csv` and `metric-run-policy.md`, which are not records, and the records that follow no code of the package
(measuring arms in `evaluation/`, solutions with no code yet). **The areas of QAR are sub-intents**: an area is a folder named by its intent id, holding the `intent.csv`, records and README of `mind`, `subconscious`, `context_retriever`, `request_intake`, `internal_context`, `critical_thinking`, `nervous_system`, `objectives`, `integrity` and `science` (under `the_intelligence/`, `the_consciousness/`, `the_body/`, `the_self/` and `the_world/`), each arising from the composition arm of its area solution (named back in that arm's `sub_intent`). The composition arm's code sits in the folder above its intent folder (`the_body/area_body.py`, `the_intelligence/subconscious/area_context_retriever.py`), so the arm's record is owned by the parent intent. The five containers are one rank in `esoi.toml` (they depend on one another in every direction). To add a record, write it in the folder of its code (or of its area), then `esoi records place` and `esoi check --write-docs`; a
program reads the records as one tree (`esoi.read_tree(folder, name)` or `esoi.registry.load`), never one CSV file. `esoi.toml` configures esoi for this repo and
`scripts/esoi_rules.py` adds QAR's own rules: S15 (environment variables are documented), Q1 (solution tags), Q2 (every metric starts model-free) and Q3 (no generic code left in QAR: a module under `quest_ai_runner/` with no Quest-domain word from `QUEST_DOMAIN_WORDS` and no `hubofhubs` import that its code uses is generic and belongs in the hub; the `Q3` rows of `esoi/waivers.csv` are the hub-migration backlog, and each check prints the waived count). Nothing lives outside a solution:
a component that serves none (S19) fails the check, and the code is removed rather than waived; a solution with no
recorded run, or a row whose measured code changed (S20), is advice that never fails the check.
**Swappable parts are declared once, beside their code.** An implementation of a port is a class with
`@implementation("<solution id>", "<name>")` (from `esoi`), and the `implementations.csv` of its folder lists the same implementation
with the file it lives in (`esoi records sync` writes a missing row; `esoi check` rule S30 fails a declaration with no
record). There is no table to keep in step: `the_self/assembler/resolve.py` is a thin entry point over `esoi.runtime` under the scope
`quest_ai_runner`, imports each implementation lazily from the file its record names, and takes the default from the
solution's `selected` (change it with `esoi select --promote`, then `esoi select --snapshot
quest_ai_runner/esoi_selection.json`, because an installed copy ships without the records; `tests/test_resolve.py` pins the
snapshot). A runner that is built from a config dataclass names it with a `config_class` attribute. To add an implementation:
write the class, add the decorator, run `esoi records sync`, fill the text, add its file to the solution's `implementations`.
`evaluation/components/esoi_behavior/` scores whether an AI's replies follow ESOI (`cases.csv` is reviewed as a sheet).

- **One shared environment:** this checkout has no virtual environment of its own. Run every command here with the one Python
  environment the deployment shares with the backend (the sibling `quest-backend/venv`, with this checkout, `esoi` and `hubofhubs`
  installed editable). After any merge or release that moves a `console_scripts` entry point or a module path, reinstall this
  checkout into it before restarting a lane: `pip install -e <this checkout> --no-deps` (a stale entry script crash-loops the lane;
  `quest-ai-runner doctor` detects it and prints the fix).
- **Before you commit:** `esoi check` (it also runs as `tests/test_structure_registry.py`; `esoi check --write-docs`
  regenerates `COMPONENTS.md`, `docs/reference/configuration.md` and `tracker/*.csv`) and `esoi hygiene`. Each problem line
  starts with the id of the rule it breaks. A changed file does not order a metric run: `esoi run --stale --plan` says which recorded
  results no longer describe the code (the changed file and the component that owns it) and why each is or is not re-run, and
  `esoi check --strict` lists them as advice that never fails. Improve first, rerun only failing cases, and measure when a decision or
  release depends on the result (`esoi run --stale --for-decision`, or name an arm with `--include component/impl`; `--all` is the old
  run-everything). No commit hook and no CI step runs metrics. A row is stale only when code the arm measures changed (esoi's
  `import_closure` rule); an arm that loads modules by a computed name is judged by its component's files in `esoi/components.csv`, so
  keep that ownership map current. Offline arms only: a live arm (model calls) is run only by decision, per `esoi/metric-run-policy.md`,
  and an offline run refuses to start a model command.
- **Hub requirements and coherence:** `quest-ai-runner doctor` (unmet requirements with their fix, a stale-console-script check, plus lock status when a lock exists)
  and `esoi requirements --package quest_ai_runner --coherence --offline` (the selected implementations resolve together; the unmet items it lists are
  keys, programs and variables, not failures). The hub-declared part of `.env.example` is generated by
  `python -m quest_ai_runner requirements --write-env-example .env.example`; `docs/how-to/keep-quest-ai-runner-current-with-the-hub.md`.
- **The hub of hubs** (`ai-hub-of-hubs-structure`, a sibling repository; package `hubofhubs`) is a REQUIRED dependency: the
  registry of AI-intelligence hubs, the seats (Protocols), the assembly and the scorer behind sub-metric QME9. QAR imports it at
  startup in one place, `quest_ai_runner/the_self/assembler/hub.py`, and registers its parts there. For dev and tests keep the sibling checkout
  beside this repository (pytest `pythonpath` in `pyproject.toml`; `evaluation/components/hub_of_hubs` also puts it on the path)
  rather than pip-installing it. QAR keeps its bindings (`esoi/hub_seats.csv`), probes and Quest adapters; registry facts are
  changed in the hub repository, never copied here (`docs/explanation/hub-of-hubs.md`). Generic assembly, selection, cascade and checking
  belong in the esoi library, not here or in the hub; see `evolvable-structure/docs/explanation/what-belongs-where.md`.
- **Extend before adding a solution:** a new behaviour is first a case, a metric stage, an arm or a flow step of an existing solution; a new solution needs a distinct purpose, competing alternatives and lasting existence (`evolvable-structure/docs/explanation/extend-before-adding-a-solution.md`).
- **New module:** put it where its role lives (existing glob usually covers it). A new role is a new
  `components.csv` row. A new implementation of a port is added to its `solutions.csv` row; select it only
  when its metric wins on the same cases.
- **Split rule:** split by role, update every importer (in this repo and in quest-backend) to the module that now
  defines the name, and leave no re-export shim behind: this is a new repository and carries no compatibility code.
  Patch tests at the module whose code looks the name up.

## 🔒 Hard rule #1: NOTHING consumer-specific or secret goes in the repo

This repo is public. The following must **never** appear in `quest_ai_runner/`, `tests/`,
`examples/`, the docs, or **the git history**:

- API keys or tokens of any kind (Quest `qsk_...` keys, `sk-ant-...`, bearer tokens, passwords).
- Real user ids, team ids, org-internal identifiers, or email addresses of real people.
- Absolute filesystem paths from a real machine (e.g. `/home/<someone>/...`).
- Any one org's names, personas, corpora, or business logic baked into the library.

All of that is **consumer config**. It comes in at runtime via `RunnerConfig` and environment
variables, see `examples/custom_consumer.py` and `.env.example`. If a change would hardcode any
of the above, route it through config or an adapter instead.

> Why this file exists: the project was extracted from an internal codebase that *did* contain
> such specifics. They were removed and the git history was reset before publication. Do not
> reintroduce them.

## 🔒 Hard rule #2: the generic boundary holds

New capabilities go **behind one of the adapter interfaces** (`RetrievalAdapter`, `ModelProvider`,
`DeepRunner`, `EscalationSink`, `ContextAssembler`, `CardRepository`, `VectorStore`,
`ConversationStore`, …) or into `RunnerConfig`, never as a special case inside the brain. The brain
must stay ignorant of who is calling it.

When we build a feature here and in a consumer we maintain (e.g. quest-backend), put the reusable
adapter implementation in THIS repo (parameterized by connection/config), not duplicated in the
consumer; the consumer just wires it. (Anyone using the repo is of course free to write their own.)

Nothing here is frozen. This is pre-release and single-consumer, so the public API in
`quest_ai_runner.api` (`StreamSink`, `MilestoneSink`, `Orchestrator` and the rest of `api.__all__`) is
free to evolve: change it whenever the design calls for it. Prefer additive, backward-compatible
changes where they're just as clean, but a breaking change is fine when it's the right shape; just
keep it generic (hard rule #2 above), update the `CHANGELOG.md` (Unreleased) and any affected docs,
and keep the tests green.

## Before you commit or push

1. **Run only the tests your change touches, never the full `tests/` suite unless a human
   explicitly asks for it, and never make a full run a blocker to commit.** Most of this suite is
   offline, but parts of it (context assembly, planner/verifier paths) make real LLM calls, so a
   full run is slow and burns real spend; one run already had to be killed mid-flight for exactly
   that reason (2026-09-19). Name the specific test file(s) your change touches and run those:
   ```bash
   python -m pytest -q tests/test_the_file_you_touched.py
   ```
   Once the targeted files pass, commit. If you want the full suite run for extra confidence, ask
   for it as its own separate step, never as a gate in front of a commit that's otherwise ready.
2. Scan your staged change for secrets/PII before committing:
   ```bash
   git diff --cached | grep -nE 'qsk_[A-Za-z0-9]|sk-ant-|/home/|@[A-Za-z0-9.-]+\.(com|org)|[0-9a-f]{24}' \
     && echo "^^ REVIEW: possible secret/PII, do not commit until cleared" || echo "scan clean"
   ```
   (`[0-9a-f]{24}` catches Mongo-style ObjectIds. A hit isn't always a problem. Review it.)
   The scan is also a command, with placeholders (`qsk_...`, `/home/user/`, `name@example.org`) let through:
   `esoi hygiene --staged` (see `docs/how-to/public-hygiene.md`).
3. Never commit a real `.env`, `qar_state.json`, or any `*_state.json` (all gitignored).
4. Keep `CHANGELOG.md` (Unreleased) and docs in sync with behavior changes.

## Process management

When running `quest-ai-runner poll` or `quest-ai-runner chat` as systemd user services:
- **Restart via systemctl:** `systemctl --user restart quest-ai-runner-*.service`
- **Do NOT kill processes directly**: use systemctl to restart cleanly
- The service file manages lifecycle; always use systemctl for start/stop/restart

## Concurrent AI work: preserve uncommitted changes

Multiple AIs may work on this repo simultaneously. **Never run destructive git operations
(`git restore`, `git reset --hard`, `git checkout .`) on files with uncommitted changes.**

1. Always check `git status` before making changes
2. If a file has uncommitted changes, they're likely in-progress work from another AI
3. Leave uncommitted changes as-is; work around them or wait for them to be committed
4. Only discard changes if you're certain they're yours and no longer needed

This prevents accidentally losing concurrent work when a user interrupts (Ctrl+C) a long-running
edit or task.

## Making LLM calls in this repo

**Always use `MultiProvider`, never call a raw provider directly.**

The deployment uses `MultiProvider` to route model calls to the right underlying provider (Anthropic, Gemini, OpenAI) based on the model name. A raw `AnthropicProvider` does not know about Gemini models and will 404. A raw `GeminiProvider` does not know about Claude models.

The correct pattern in any code that needs to make an LLM call:

```python
# In CLI / entry-point code: call build_orchestrator() first so cfg.model_provider
# gets wrapped with MultiProvider, then use cfg.model_provider for all calls.
from .config import build_orchestrator, _config_from_env
cfg = _config_from_env()
build_orchestrator(cfg)          # wraps cfg.model_provider with MultiProvider in place
provider = cfg.model_provider    # now a MultiProvider that routes by model name prefix

from .core.model_registry import ModelRegistry
model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")
result = provider.answer([{"role": "user", "content": prompt}], model=model)
```

**Always use `resolve_tier()` for the model: never hardcode a model name, and never call `list_models()[0]`** (that returns the first model from the Anthropic API which may not be routable by the current provider config).

**Tier guidance:** a call names its role (`resolve_role(call_id)`); the hub's role table gives the tier (`docs/how-to/writing-a-consumer.md`, "Which tier each model call runs on"). Classification is fast, a response a person reads is at least balanced, `"best"` only where a person set it or for `structure-watch`. The `model_fallback` config maps tiers to real model IDs.

**Never hand a model a whole collection** (Joshua, 2026-11-13: "must NEVER require ALL context to be read by LLM, where that context is infinite like goals, notes, unrelated quests"). Goals, notes, conversation turns, quests, cards and files are unbounded: before any prompt, filter by structure (a time window, statuses, scope), rank what is RELEVANT by rarity-weighted words, and when the relevant set still exceeds the token budget show its REPRESENTATIVES (TF-DF-IDF, `select_representatives`) plus a one-line digest of what was left out, all through the hub's `shortlist` / `plan_retrieval` (`hubofhubs.api`). A read the code does for counting may fetch everything in one call (`list_quest_goals` with `everything`), a prompt never lists everything. An unread source is `None`, never empty. Standard: `docs/standards/bounded-model-context.md`; example: `QuestRetrievalAdapter.bounded_goal_lines`.

**JSON parsing:** LLM responses often include markdown fences. Never call `json.loads(raw)` directly: strip fences first with a helper like `_extract_json()` in `core/card_filter.py`.

## 🔒 Hard rule #3: do not gate decisions on keywords in AI output

**Never make a control-flow or safety decision by scanning the model's own generated text for a
fixed list of words** (a denylist of "risky" verbs, an allowlist of "safe" ones, "if the confirm
question contains `delete`/`send`/…" and so on). This pattern is brittle by construction: the model
phrases its output arbitrarily, so any fixed list silently leaks every case it did not anticipate.
A real example that was removed: a `_confirm_is_redundant` gate auto-executed a planner's confirm
(skipping the human-in-the-loop approval) unless a denylist word appeared in the request or in the
planner's own confirm question, so "share this doc with the team" and "invite Jane to the quest"
auto-executed, because `share`/`invite`/`grant`/`message`/`gift`/`transfer` were not on the list.
Flipping it to an allowlist does not fix it; both make safety depend on the model happening to use
a word the code guessed.

Instead:

- **Honor the model's structured decision.** If the planner emits `action: "confirm"`, that IS the
  decision that this needs a human: surface it. Do not second-guess it by inspecting the wording
  of its message.
- **If the model decides wrong too often, fix the model, not the output.** Improve the planner's
  prompt / instructions / the schema it fills in, so the decision it returns is the right one.
  Keyword post-processing of model output is not an acceptable substitute.
- Reading the **user's own words** to interpret THEIR intent is different and fine (that is what
  `_message_requests_change` and `judge_execution_directive` do on the user message). The rule is
  specifically about gating on words the *model* generated.

## 🔒 Hard rule #4: a second consumer's need belongs in the library, not copied into a consumer

**If a second lane would need it, it goes in `quest_ai_runner`, not in a consumer's own file.**
This repo exists because generic, reusable plumbing kept getting written once, privately, per
consumer, instead of once, here, for everybody. Three separate consumers each hand-rolled the same
executor-lane entry point (the `--check`/`--once`/loop-forever driver around `Poller`); two of them
noticed the duplication and extracted a shared copy of it, but OUTSIDE this library, in a path only
their own two lanes could import, so the third consumer had nowhere to find it and wrote a fourth
copy from scratch. Separately, two consumers independently wrote persona-resolution machinery (a
registry of ids to skill folders, plus a policy for picking one from a task) with different
capabilities that never met, until both were rebuilt as one generic module.

**The test to apply:** before writing a helper, a resolver, a CLI loop, or a config-loading
function inside a consumer, ask "would a second lane need this?" If yes, it is not consumer logic;
it is a gap in this library, and the fix is here:

- A shared entry-point shape, loop, or process-lifecycle concern → `the_consciousness/request_intake/lane.py` (or a new
  module next to it), not a copy in your consumer. Start from
  [`docs/tutorials/tutorial-your-first-lane.md`](docs/tutorials/tutorial-your-first-lane.md).
- A policy for deciding WHO or WHAT handles a task, expressible as data rather than one lane's
  bespoke code → a `RunnerConfig` field with a resolver function beside it (see
  `the_consciousness/representatives/personas.py`), documented in [`docs/how-to/personas.md`](docs/how-to/personas.md), not a hand-written
  callable duplicated across consumers.
- Anything else generic → an adapter interface (hard rule #2) or a new `RunnerConfig` field, per
  the usual rule.

This does not mean invent generality nobody asked for: a genuinely one-off piece of a single
consumer's business logic stays in that consumer, same as always. It means: when you notice the
SAME piece of plumbing being built twice, the second build is the signal to move it here instead of
finishing a second private copy.

## Conventions

- Match the surrounding code's style, naming, and comment density.
- Commit in this repo only; write clear, scoped commits.
- Tests for any behavior change, and they must pass offline.
- **Every file lives in its proper folder, never the repo root.** This applies to ALL files, not
  just tests: library code under `quest_ai_runner/`, tests under `tests/`, runnable reference
  consumers under `examples/`, docs under `docs/`, evaluation harnesses under `evaluation/`. The
  repo root is for project config only (`README.md`, `CHANGELOG.md`, `pyproject.toml`, `.env*`,
  etc.): do not drop new `.py` files there.
- **Throwaway / scratch / demo scripts go in the gitignored `scratch/` directory** (create it if
  absent; it is in `.gitignore`), never the repo root and never committed. Delete them when done.
