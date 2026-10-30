# Lean launch of the deep worker: an experiment spec (not run)

Question: does launching the deep worker (`claude -p`) with the lean flags the chat provider already uses, and with the quest's
context cards in the brief, cost fewer tokens per goal that is actually met, without losing goals?

Status: **specified, implemented, held.** Nothing here has been run. The runs are subscription (`claude -p`) runs, so they wait for
the standing hold on costly tests to lift (`esoi/metric-run-policy.md`, the backlog HOLD line); a run needs a decision, not a flag.
Neither script in this folder runs a model.

## The parameters (one implementation, different settings)

The launch is one function, `core/deep_launch.build_deep_argv`, over a frozen `DeepLaunchSettings`. Today's launch is the default
(every field unset, no flag added; `tests/test_deep_launch.py` pins the default argv byte for byte). An arm is a setting, never a
second runner, in the `name@k=v` spirit of esoi variants.

| Field | Flag | Environment name (read in QAR's settings code) |
|---|---|---|
| `tools` | `--tools "A,B"` (only these built-in tools, so only their schemas) | `QAR_DEEP_TOOLS` (`none` = no tools) |
| `setting_sources` | `--setting-sources <list>` (`""` loads none) | `QAR_DEEP_SETTING_SOURCES` (`none` = none) |
| `strict_mcp` | `--strict-mcp-config` | `QAR_DEEP_STRICT_MCP` |
| `exclude_dynamic_sections` | `--exclude-dynamic-system-prompt-sections` | `QAR_DEEP_EXCLUDE_DYNAMIC_SECTIONS` |
| `max_budget_usd` | `--max-budget-usd <n>` | `QAR_DEEP_MAX_BUDGET_USD` |
| `max_turns` | `--max-turns <n>` (replaces the call's budget) | the existing `QAR_DEEP_MAX_TURNS` for the loop; `SubprocessConfig.launch` for the launch |

Settable too through the runner config: `SubprocessConfig.launch` (wins over the environment). Each flag was checked against
`claude --help` (2.1.314) except `--max-turns`, which that text does not list but this runner already passes. One finding: the help
says `--exclude-dynamic-system-prompt-sections` is "ignored with `--system-prompt`", and the deep runner passes no `--system-prompt`,
so the flag applies here (the chat provider passes one, so for it the flag is inert).

## Arms (fixed in `decide.py`)

| Arm | Launch |
|---|---|
| A | today: no lean flag |
| B | lean launch: `--tools "Bash,Read,Edit,Write,Glob,Grep"`, `--setting-sources ""`, `--strict-mcp-config`, `--exclude-dynamic-system-prompt-sections`, `--max-budget-usd 3` (a safety ceiling; a run that reaches it is not met) |
| C | B, plus the goal's top context cards in the brief (retrieved before the run by the offline lexical assembler, k = 8, the same cards for every repeat), plus `--max-turns 6` |

Arms D to F of the research (output-filter hook, a cheap-model harness, FastEdit) are not part of this experiment.

## Cases

`build_cases.py` chooses 12 real small and medium commits of this repository (6 changing at most 40 library source lines, 6 changing
41 to 200), picked by a hash of the commit id so no recency or author preference enters. Each becomes a scratch copy at the commit
with the library source put back to its parent's state and the commit's own tests kept. The goal is "these tests fail, change the
source so they pass, do not edit the tests" plus the commit subject. **The oracle is the commit's tests** run in the copy; the
worker's own claim never counts as met. `--verify` proves each case fails before the work and passes on the original, and a case
that does not discriminate is dropped and replaced before any run. The existing seven `claude_code_ab` locate cases may be added as
a second set; they are not needed for the rule below.

Ladder: 3 cases, then 10, then all 12 (a rung is run only if the one before it showed no breakage); each arm on the same cases,
one run per case per arm unless a rung shows variance worth a repeat.

## The record of one run

From the worker's JSON envelope and the oracle (`decide.RECORD_FIELDS`): `met` (oracle), `claimed_met` (the worker's result),
`false_done` (claimed met, oracle failed), `turns`, `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `output_tokens`,
`wall_seconds`, `total_cost_usd` (an estimate; the plan-usage share is what matters on a subscription). Recorded rows are committed
only after the scorer was checked by hand against a few runs.

## Decision rule (pre-registered, `decide.decide`)

An arm replaces the default launch only if **both**:

1. its met count is within one task of A's (`met_arm >= met_A - 1`), and
2. its tokens per met goal are at least 30 percent lower than A's. Tokens are the sum of input, cache-read, cache-creation and
   output tokens over all runs of the arm, divided by the number of goals the oracle says were met.

An arm that met nothing, or arms run on different case counts, never replaces. Reported beside the rule, not part of it: cost per
met goal, wall seconds, and false-done counts (an arm with more false-done than A is flagged for a human to read before any
change). The rule is not changed after seeing results; a different rule is a new experiment. If B passes and C does not, B alone
becomes the default; the default changes by setting the `QAR_DEEP_*` names or the config, and never by editing the launch code.
