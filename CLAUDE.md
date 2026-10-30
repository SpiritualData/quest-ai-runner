# CLAUDE.md: working rules for this repository

This file orients any contributor (human or AI) working in `quest-ai-runner`, and encodes the
**hard rules** that keep this public, open-source repository clean. Read it before making changes.

## What this project is

`quest-ai-runner` is **Quest's connector** and the production consumer of the hub of hubs (`hubofhubs`, a required
dependency; `quest_ai_runner/hub.py` imports it at startup and registers QAR's parts under names QAR owns): the
orchestrator **brain** (`quest_ai_runner.core`) plus the queued-task **executor** (`quest_ai_runner.runner`) for Quest AI
tasks. It is published as open source (Apache-2.0) and is meant to run for *any* org, not one specific deployment. The
brain (`core/`) stays free of Quest; the Quest-specific parts are the adapters, the runner and the config. Generic engines
are moving into the hub seat by seat (`docs/hub-of-hubs.md`); QAR keeps what knows Quest.
- `core/`: the brain, a bounded plan → gather → re-plan → answer/deep/confirm loop. Imports
  nothing about Quest, any database, any org, or any user. Depends only on the adapter
  *interfaces* (`Protocol` ports) it defines in `core/adapters*.py`; `esoi/solutions.csv` lists them all.
  Prompt text is split out by definition: the planner prompt (rubric, full/compact variants, mode
  notes, profile table) is `core/planner_prompt.py`; `orchestrator.py` holds the loop and re-exports
  those names. A new prompt variant is an entry in `PLANNER_PROFILES`; add prompt text there, not to
  the loop.
  The other prompt text and tool schemas (goal verification, intent and mode-release directives,
  future-context asks, card updater, reply voice, post-work synthesis) are `core/auxiliary_prompts.py`;
  how gathered observations, links and past escalations are rendered into the planner's view is
  `core/planner_context.py`. Both are re-exported from `orchestrator.py`; put new prompt text or
  rendering helpers in those modules, not in the loop.
  The `Orchestrator` class itself is split by role into mixins (`orchestrator_reads`, `_planning`,
  `_answering`, `_judges`, `_deep_models`, `_deep_context`, `_deep_run`, `_cards`, `_oversee`,
  `_understanding`, `_run`, `_recipes`); `orchestrator.py` keeps `__init__` and re-exports every
  moved name, and the module-level pieces live in `orchestrator_constants`, `_types`,
  `_decide_tool`, `_decisions`, `_text`, `_events`, `_capabilities` and `_turn`. Add a method to
  the mixin that owns that role, not to `orchestrator.py`. `RunMixin.run` is a short dispatcher: its
  turn is a chain of phase methods (`_run_begin`, `_run_plan_loop`, ...) held in `orchestrator_run_begin`,
  `_loop`, `_answer` and `_close`, which share one run-local namespace `rs`. The ports (`core/adapters_*`) and
  `core/goal_runner.py` are split the same way (`adapters_*`, `goal_runner_*`); importers name the defining module, and
  `core/adapters.py` re-exports only the names the Quest backend imports from it. `core` never imports
  outward: what it needs from `adapters`/`runner` is injected, and `quest_ai_runner/__init__.py`
  registers `cli_model` (`goal_runner.set_cli_model_translator`) and the Quest client factory
  (`tools.set_quest_client_factory`). The same pattern applies to
  `adapters/file_context_store.py` (`context_*` modules), `runner/quest_client.py`
  (`quest_client_*`), `runner/context_updates.py` (`context_update_*`, `context_sources_*`),
  `runner/autopilot.py` (`autopilot_*`), `runner/poller.py` (`poller_*`), `runner/executor.py`
  (`executor_*`), `config.py` (`config_*`), `cli.py` (`cli_*`: one module per subcommand family, `cli.py` keeps `main()`),
  `textual_ui.py` (`textual_ui_*`: widgets plus `QuestAITerminal` mixins) and `interactive_session.py`
  (`interactive_session_*`). A test that monkeypatches a module global patches the
  module whose code looks the name up, so patch the module that now holds the function.
- `runner/`: the executor: poll → claim → run → escalate → report.
- `adapters/`: reference *implementations* of the interfaces: retrieval (`FilesAdapter`,
  `CachedDbAdapter`, `CompositeRetrievalAdapter`, MCP, Notion, Google), model providers (Anthropic,
  Gemini, OpenAI, DeepSeek, Claude CLI, behind `MultiProvider`), card stores, vector and hybrid context
  assemblers, web research, deep runners and conversation stores. `esoi/COMPONENTS.md` is the
  current list.
- `config.py`: `RunnerConfig`, the single place a consumer supplies everything specific.
- `examples/`: runnable reference consumers (env-driven, no real data).
- `docs/`: tutorial + how-tos + architecture.

Module names describe **architectural role**, not domain. "core" is the stable center everything
depends on; "runner" is the executor; "adapters" are the swappable implementations. See
`docs/ARCHITECTURE_STANDARDS.md` for the full architecture and the standards code here must follow.

## Evolvable structure (ESOI): the registry and the check

The repo's structure is data, not folklore, and the checking, running and measuring of it is Joshua's `esoi` package
(a dependency pinned in `pyproject.toml`, a runtime one because implementations are declared with its decorators; `esoi rules`
lists its rules, so they are not restated here, and the steps and the philosophy are the library's docs and the
evolvable-structure skill, not copied here). `esoi/`
holds `components.csv` (placement only), `intent.csv` (the top intent: why this project exists, in its own words) beside the five intent record files,
`waivers.csv` and `metric-run-policy.md`; `esoi.toml` configures esoi for this repo and
`scripts/esoi_rules.py` adds QAR's one rule (S15, environment variables are documented). Nothing lives outside a solution:
a component that serves none (S19), or a solution with no current recorded run (S20), fails the check, and the code is
removed rather than waived.
**Swappable parts are declared once, beside their code.** An implementation of a port is a class with
`@implementation("<solution id>", "<name>")` (from `esoi`), and `esoi/implementations.csv` lists the same implementation
with the file it lives in (`esoi records sync` writes a missing row; `esoi check` rule S30 fails a declaration with no
record). There is no table to keep in step: `core/resolve.py` is a thin entry point over `esoi.runtime` under the scope
`quest_ai_runner`, imports each implementation lazily from the file its record names, and takes the default from the
solution's `selected` (change it with `esoi select --promote`, then `esoi select --snapshot
quest_ai_runner/esoi_selection.json`, because an installed copy ships without the records; `tests/test_resolve.py` pins the
snapshot). A runner that is built from a config dataclass names it with a `config_class` attribute. To add an implementation:
write the class, add the decorator, run `esoi records sync`, fill the text, add its file to the solution's `implementations`.
`evaluation/components/esoi_behavior/` scores whether an AI's replies follow ESOI (`cases.csv` is reviewed as a sheet).

- **Before you commit:** `.venv/bin/esoi check` (it also runs as `tests/test_structure_registry.py`; `esoi check --write-docs`
  regenerates `COMPONENTS.md`, `docs/configuration.md` and `tracker/*.csv`) and `.venv/bin/esoi hygiene`. Each problem line
  starts with the id of the rule it breaks. After committing measured code or cases (`quest_ai_runner/`,
  `evaluation/components/`), run `esoi run --stale` and commit `evaluation/components/results.csv`, then `esoi check --strict`.
  Offline arms only: a live arm (model calls) is run only by decision, per `esoi/metric-run-policy.md`.
- **The hub of hubs** (`ai-hub-of-hubs-structure`, a sibling repository; package `hubofhubs`) is a REQUIRED dependency: the
  registry of AI-intelligence hubs, the seats (Protocols), the assembly and the scorer behind sub-metric QME9. QAR imports it at
  startup in one place, `quest_ai_runner/hub.py`, and registers its parts there. For dev and tests keep the sibling checkout
  beside this repository (pytest `pythonpath` in `pyproject.toml`; `evaluation/components/hub_of_hubs` also puts it on the path)
  rather than pip-installing it. QAR keeps its bindings (`esoi/hub_seats.csv`), probes and Quest adapters; registry facts are
  changed in the hub repository, never copied here (`docs/hub-of-hubs.md`). Generic assembly, selection, cascade and checking
  belong in the esoi library, not here or in the hub; see `evolvable-structure/docs/explanation/what-belongs-where.md`.
- **New module:** put it where its role lives (existing glob usually covers it). A new role is a new
  `components.csv` row. A new implementation of a port is added to its `solutions.csv` row; select it only
  when its metric wins on the same cases.
- **Split rule:** split by role, keep the old module as the stable import path that re-exports every moved
  name, and patch tests at the module whose code looks the name up.

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
`quest_ai_runner.core` (`Mode`, `StreamSink`, `MilestoneSink`, `ProgressEvent`, `Orchestrator`) is
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
   `.venv/bin/esoi hygiene --staged` (see `docs/public-hygiene.md`).
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

**Tier guidance:** use `"balanced"` for filtering/judgment tasks (Gemini Flash class), `"fast"` for cheap lookups. `"best"` for high-stakes reasoning. The `model_fallback` config maps these to real model IDs.

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

- A shared entry-point shape, loop, or process-lifecycle concern → `runner/lane.py` (or a new
  module next to it), not a copy in your consumer. Start from
  [`docs/tutorial-your-first-lane.md`](docs/tutorial-your-first-lane.md).
- A policy for deciding WHO or WHAT handles a task, expressible as data rather than one lane's
  bespoke code → a `RunnerConfig` field with a resolver function beside it (see
  `runner/personas.py`), documented in [`docs/personas.md`](docs/personas.md), not a hand-written
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
