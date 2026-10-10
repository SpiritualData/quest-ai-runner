# Recipes: replay an operation you already worked out

A recipe is a tool call that already succeeded, remembered with the request that caused it. A later
request for the same kind of operation runs it immediately, before understanding and before any
context search, and the tool's receipt is the answer. Code: `quest_ai_runner/core/recipes.py`.

## Turning it on

* Library: `Orchestrator(..., recipes=RecipeStore(path))` (or `RunnerConfig.recipe_store`).
* Environment: `QAR_RECIPES=1`; `QAR_RECIPES_DIR` (default `<QAR_CORPUS_ROOT>/.quest-context/recipes`);
  `QAR_RECIPES_MIN_SCORE` (default 0.5).
* `OrchestratorConfig.recipe_fast_path` (default true) switches it off per orchestrator, and
  `recipe_allow_mutating` (default true) limits it to read-only tools when false.

Off means the run is unchanged. The fast path applies to typed, live user turns without attachments.

## How a turn goes

1. `RecipeStore.match` compares the request's content words with each saved example request (token
   Jaccard, no model, no index) and nominates recipes above the floor. Scope tags are honored.
2. For the best nominee, ONE fast-tier call answers `{applies, args}`: is this the same operation,
   and what arguments? A read-only recipe asked in exactly the saved words skips even that call.
   A mutating recipe always has its arguments re-derived.
3. The tool runs through `ToolRegistry.invoke`. Success: the receipt is the answer and the turn
   ends (`exit_reason == "recipe"`). Anything else (`applies` false, a tool failure, an exception):
   the normal path runs as if nothing had happened.

## How a recipe is learned

Every successful planner `tool` action in a typed user turn is saved by `RecipeStore.learn`. Nothing
is learned from a failure, from a queued brief, or from a request with fewer than two content words.

## Not covered

Quest's chat runs most writes as generated Python over its own data, not as tools, so those
operations are not recipes yet. See `evaluation/recipe_fast_path_eval.py` for the measurement.
