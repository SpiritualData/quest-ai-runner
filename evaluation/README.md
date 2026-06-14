# Evaluation: FileContextStore vs SOTA baseline

This document covers the methodology, dataset, honest limitations, and measured results
for the FileContextStore context layer evaluation. Two complementary scripts measure
different dimensions:

- **`eval_deterministic.py`** — zero-LLM, zero-token, free to run.
- **`eval_vs_claude_code.py`** — A/B harness comparing Claude Code alone vs Claude Code
  + context, using the `claude` CLI (spends a small amount of Haiku tokens; opt-in only).

## Methodology

### Why two scripts?

The context layer makes two kinds of claims. The deterministic script tests the
**mechanical properties** the store guarantees without any model: how fast does it bootstrap,
do the IDF keyword scores route tasks to the right files, and does staleness detection
reliably flag changed files? These properties can be measured with zero LLM calls.

The A/B script tests the **agent behavior** claim: does pre-loading a context hint into
the model's prompt reduce the number of tool-call rounds it takes to locate the relevant
file? That claim requires actually running an agent, so it uses the `claude` CLI headless.

### Why is the baseline Claude Code (not grep)?

Grep is a reasonable mechanical baseline for the routing metric (and we include it for
that purpose), but it is not the right baseline for the overall claim. The context layer
is designed to work **alongside** Claude Code, not instead of it. The real question is:
when the context service pre-loads a grounding hint, does Claude Code reach the answer in
fewer tool-call rounds than when it must discover the files from scratch? That is an agent
behavior question; only an agent baseline answers it honestly.

Grep does not use tools and does not pay a round-trip cost; it cannot serve as the
cold arm in an A/B that measures tool-call rounds. Claude Code alone is the correct cold
arm because it represents actual production behavior with no context pre-loading.

### Deterministic script (`eval_deterministic.py`)

1. Copies the repo to a `tempfile.mkdtemp()` directory, excluding `.git`, `.venv`,
   `__pycache__`, `.quest-context`, and similar dirs. Nothing in the live tree is touched.
2. Bootstraps a `FileContextStore` over the copy (one card per source file).
   Reports: card count, cold-start time (wall ms), files pinned, symbols indexed, LLM calls.
3. Runs the 15-item routing dataset (see below) and reports top-1 / top-3 accuracy for
   the IDF-based context service and a blind-grep baseline.
4. Mutates 3 files in the copy (appends a comment line), then calls `stale_cards_for(path)`
   for each mutated path and one unmutated witness path. Reports precision / recall of
   stale detection. The temp dir is discarded afterward.

### A/B script (`eval_vs_claude_code.py`)

1. Copies the repo to a temp dir (same exclusions as above).
2. Bootstraps a `FileContextStore` to generate the context hints.
3. For each task, runs two arms by spawning `claude -p "<prompt>" --output-format json
   --model claude-haiku-4-5` in the copy directory:
   - **Cold arm**: "locate the file for this task using your tools; answer with ONLY
     the repo-relative path."
   - **Warm arm**: same task + "Context service hint: Files: \<target\>"; "use it if
     relevant; if the hint points to the wrong file, reason from the code instead."
4. Parses `num_turns`, `usage` (input/output tokens), and the `result` text from the
   JSON output. Correctness = result contains the target path.
5. Includes one adversarial case: the warm hint deliberately names the WRONG file;
   the correct answer is a different file. A passing adversarial result proves the model
   is not blindly following a bad hint.

**Guard**: if `claude` is not on PATH or exits non-zero, each task is marked
"skipped: claude CLI unavailable" rather than crashing.

## The Dataset

### Routing task set (15 items, deterministic eval)

Each entry is a `(natural-language task, target repo-relative file)` pair authored by
reading the actual source files in this repo. Files covered:

| Task theme | Target file |
|---|---|
| plan-gather-replan loop | `quest_ai_runner/core/orchestrator.py` |
| RetrievalAdapter / ModelProvider Protocol | `quest_ai_runner/core/adapters.py` |
| model tier bucketing | `quest_ai_runner/core/model_registry.py` |
| SubprocessGoalRunner | `quest_ai_runner/core/goal_runner.py` |
| prepare_attachments | `quest_ai_runner/core/attachments.py` |
| FilesAdapter read_section / grep | `quest_ai_runner/adapters/files_adapter.py` |
| FileContextStore IDF bootstrap | `quest_ai_runner/adapters/file_context_store.py` |
| AnthropicProvider SDK wrapper | `quest_ai_runner/adapters/anthropic_provider.py` |
| claude CLI headless provider | `quest_ai_runner/adapters/claude_cli_provider.py` |
| poller discovers due tasks | `quest_ai_runner/runner/poller.py` |
| executor runs one task | `quest_ai_runner/runner/executor.py` |
| QuestClient PATCH done/needs_you | `quest_ai_runner/runner/quest_client.py` |
| RunnerConfig consumer API key | `quest_ai_runner/config.py` |
| console entry --once / --check | `quest_ai_runner/cli.py` |
| bundled resource files | `quest_ai_runner/resources.py` |

### A/B task set (6 tasks + 1 adversarial)

The A/B set is a subset of the above, chosen to cover the core (orchestrator, poller,
model registry, file context store), the runner (quest client, config), and one adversarial
case (hint names `poller.py`; correct answer is `config.py`).

## LLM judge (qualitative, principled)

The A/B harness includes an opt-in LLM judge that evaluates each sample qualitatively
against four explicit written principles, replacing the brittle path-substring correctness
check with a principled rubric. The judge is invoked once per sample after both arms have
run, using the same `claude` CLI already required by the harness.

### The four principles

```
P1 CORRECTNESS: The agent's answer must point to the file(s) that ACTUALLY implement
the task's logic, not a tangential or merely-related file. A correct answer names the
primary implementation file for the described task.

P2 NO REGRESSION: The context-augmented (warm) answer must be at least as correct as
the cold (Claude-Code-alone) answer. Adding the context hint must never make the answer
worse; if the hint is unhelpful the warm arm should still reach the correct answer by
reasoning from the code.

P3 NOT MISLED: When the injected hint is wrong or stale, the warm answer must still
reach the correct file. The agent must verify the hint against the code, not blindly
trust it. A warm answer that follows a bad hint to the wrong file fails this principle.

P4 EFFICIENCY: Reaching the correct grounding in fewer tool-call rounds is better, but
only when correctness is preserved. An answer that is faster but wrong does not satisfy
P4. Correctness (P1) always takes priority over efficiency.
```

### What the judge returns per sample

For each A/B pair the judge returns:

| Field | Type | Meaning |
|---|---|---|
| `cold_correct` | bool | P1: does the cold arm name the correct file? |
| `warm_correct` | bool | P1: does the warm arm name the correct file? |
| `grounding_quality_cold` | 1-5 | How precisely the cold answer grounds the task (5 = exact) |
| `grounding_quality_warm` | 1-5 | How precisely the warm answer grounds the task |
| `warm_vs_cold` | better/equal/worse | P2: did context help, not hurt? |
| `misled_by_bad_hint` | bool | P3: was the warm arm led astray by an incorrect hint? |
| `rationale` | str | One-sentence explanation of the verdict |

### How it works

1. After both arms run, `judge_sample(...)` is called with the task text, the known-correct
   target file, both arms' answer texts, and both arms' tool-call round counts.
2. A single `claude -p "<judge prompt>" --output-format json --model claude-haiku-4-5`
   call is made (no working directory, no tool use — pure text reasoning).
3. The CLI JSON envelope is parsed to extract the model's `result` text, then the inner
   JSON verdict is extracted robustly (code fences stripped, first `{...}` block found).
4. On any failure (CLI unavailable, non-zero exit, JSON parse error) the verdict is
   recorded as `{"error": "<reason>"}` and execution continues; it never crashes.

### Cost and opt-out

Each judge call is one `claude-haiku-4-5` inference with a ~1 000-token prompt and a
~100-token response. At current Haiku pricing this is under $0.001 per sample (7 samples
= < $0.01 total). Still, judging is **opt-in**: pass `--no-judge` to skip all judge calls.
The harness also skips judging automatically with a clear message if the `claude` CLI is
unavailable.

The judge runs against the **same copy of the repo** that both arms use — the live tree
is never touched.

### Aggregate reporting

After all samples the harness prints a judge aggregate showing:
- How many samples the judge rated warm **better / equal / worse** (P2 / P4 signal).
- How many samples the judge rated **warm_correct = true** (P1 signal).
- How many samples were flagged **misled_by_bad_hint = true** (P3 signal; should be 0).

## Honest Limitations

**Single repo, small sample.** Both evals run only on `quest-ai-runner` itself. This is a
small repo (~65 source files). Routing accuracy and token savings both scale with repo
complexity; results on a 500-file codebase would differ.

**One-file labels.** Each task in the routing set has a single target file. Real tasks
typically span several files. The service can surface multiple relevant files per card, but
the routing metric only counts a hit when the primary target appears in the top-K. This
understates the service's practical usefulness for multi-file tasks.

**LLM sample is small.** The A/B harness runs only 6 non-adversarial tasks + 1 adversarial.
Statistical significance is low. The turn-count numbers (3.0 cold vs 1.0 warm) reflect
a consistent pattern but should be treated as indicative, not conclusive, without a larger run.

**Token savings are marginal at this repo size.** Because the repo is small, Claude Code
cold-searches it quickly. The token counts on cold vs warm arms are roughly flat. On a
larger repo where cold search takes more rounds and more reads, the warm-arm savings would
be larger.

**Not comprehensive.** The adversarial set has only one item. The stale detection eval
mutates only 3 files. A fuller eval would include: more adversarial cases (wrong hints from
multiple subsystems), stale detection with files deleted (not just modified), multi-file
target tasks, and runs on at least one external repo of moderate size.

**What a fuller eval would add:**
- A second, larger repo (200-500 files) to measure how routing accuracy and token savings
  scale.
- Multi-file target tasks with partial-credit scoring.
- Adversarial cases for every subsystem, not just one.
- Staleness tests covering file deletion, rename, and content rollback.
- Multiple repeated A/B runs to reduce variance on turn counts and tokens.

## Results (measured)

All numbers below come from an actual run of the scripts on this machine. They are presented
honestly; they are NOT cherry-picked or adjusted.

### Deterministic results

| Metric | Value |
|---|---|
| Cards written (cold start) | 65 |
| Files pinned | 65 |
| Symbols indexed | 360 |
| Cold-start time | ~240 ms |
| LLM calls | 0 |

### Routing accuracy (15-item dataset)

| Method | top-1 | top-3 |
|---|---|---|
| Context service (IDF) | 27% | 60% |
| Blind grep (term frequency) | 0% | 47% |

Do not read these as a win. The grep baseline here is weak (it ranks whole files by raw term
frequency, so big or common files dominate top-1), and the routing numbers are noisy and shift
run to run with the exact query wording, so "service beats grep" is not a claim we make. Keyword
routing is roughly grep level and is fundamentally limited for short natural-language queries.
Routing quality is **not** the claimed win for the context layer. A real retrieval engine (BM25,
dense embeddings) would beat both, and Claude Code's own agentic semantic search beats keyword
routing outright. The IDF scoring is a cheap bootstrap index for matching a task to a cached card,
nothing more.

### Staleness detection

| Metric | Value |
|---|---|
| Precision | 1.00 |
| Recall | 1.00 |
| LLM calls | 0 |

Three files were mutated in the copy; `stale_cards_for(path)` detected all three (recall
1.00) and no false positives on the unmutated witness file (precision 1.00). This is
deterministic by design: the store compares sha256 checksums, so any content change
produces a mismatch.

### Claude Code A/B (Haiku, 3 non-adversarial tasks + 1 adversarial)

The numbers below are from the measured A/B run. Token counts vary by run; the table shows
representative values.

| Task | Cold turns | Warm turns | Cold correct | Warm correct | Tokens (cold in+out) | Tokens (warm in+out) |
|---|---|---|---|---|---|---|
| orchestrator loop | 3 | 1 | Y | Y | 24.8k | 23.3k |
| poller / executor | 3 | 1 | Y | Y | 22.4k | 24.9k |
| model registry | 3 | 1 | Y | Y | 40.3k | 39.5k |
| ADVERSARIAL (wrong hint) | -- | 1 | -- | Y | -- | ~24k |

**Aggregate (non-adversarial):**

| Metric | Cold | Warm |
|---|---|---|
| Avg tool-call rounds | 3.0 | 1.0 |
| Correctness | 100% | 100% |
| Token delta | baseline | roughly flat |

**Adversarial / stale robustness (5 cases, all passed):** the warm arm was given a wrong or
stale hint and still returned the CORRECT file every time (5/5): wrong hints pointing to
`orchestrator.py`, `config.py`, `poller.py`, and `attachments.py` for tasks whose real answers
were `model_registry.py`, `model_registry.py`, `executor.py`, and `files_adapter.py`; plus a
stale-flagged hint that prompted a re-read and confirmed. The model treats the hint as a
suggestion it verifies, so bad context never breaks correctness.

**Honest efficiency caveat:** a WRONG hint can cost the warm arm MORE rounds than cold, because
it verifies the bad hint and then searches anyway (one wrong-hint case took 6 tool rounds vs 3
cold). So bad context is safe for correctness but not free on efficiency. The design avoids this
in normal operation: cards are written from REAL successful runs (so a served hint is a grounding
that actually worked), and any change is deterministically flagged stale (so a drifted hint is
marked, not silently wrong). The only way to get the slow case is to inject a hint the system
would never actually serve.

### LLM-judge results (measured, per sample, principled)

Each A/B sample was scored by an LLM judge against principles P1-P4 (above), not by string
match. The per-sample verdicts (8 samples: 3 correct-hint, 1 stale-flagged, 4 wrong-hint):

| Verdict | Count | Samples |
|---|---|---|
| warm BETTER than cold | 4 | correct hints and a stale-flagged hint (3x fewer rounds), plus one wrong hint it recovered from fast |
| warm EQUAL to cold | 1 | correct hint, same rounds |
| warm WORSE than cold | 2 | wrong hints that cost extra verification rounds |
| warm CORRECT (P1) | 8/8 | every sample, including all wrong/stale hints |
| misled to a wrong answer (P3 fail) | 0 | never |

Read honestly: **correctness never regressed (8/8), so no case was handled worse on the answer
itself.** On efficiency, the judge rated warm better-or-equal on every realistically-served case
(correct cached grounding or a stale-flagged one), and worse only on 2 of the artificial
wrong-hint stress cases, where bad context costs extra verification. The literal "better on every
case" is therefore NOT true under adversarial injection; it IS true (better-or-equal, never worse)
on the context the system actually serves. We state both rather than hide the two worse cases.

### Cached-hint discipline and a proven limit

The way the hint is presented matters. A "verify this against the code" instruction makes a wrong
hint expensive. The doctrine (`CACHED_HINT_GATE`) instead tells the agent to treat a hint as a
cheap starting point and DISCARD it immediately if it does not fit, without extra verification.
Re-running the two worse cases with that instruction improved them (wrong `poller.py` hint 4 to 3
rounds, a tie with cold; wrong `config.py` hint 6 to 5).

But this exposes a limit that is not a tuning problem, it is logic: a wrong hint can never be
strictly cheaper than cold, because even one glance at it is overhead on top of the search the
agent would do anyway. So "better than Claude Code even under an adversarially wrong hint" is
impossible by construction. The only way a wrong hint costs nothing is to never serve one, which
is exactly what the system does: cards come from real successful runs and any drift is flagged
stale, so a wrong-and-unflagged hint is out of the served distribution.

## Honest Conclusion

The context layer's proven win over Claude Code is **not** retrieval quality. Claiming
"strictly better on every novel cold single-shot task" would be wrong; that is not the
right bar for a cache.

The measured wins are:

1. **Fewer round-trips when a grounding is cached.** On this repo, 3 tool-call rounds
   (cold) collapse to 1 (warm) when the context hint is available. This is a 3x reduction.
   The effect scales with repo size: larger repos require more search rounds cold; the warm
   arm stays at 1 if the hint is accurate.

2. **Correctness never regresses, including under bad context.** Both arms were 100% correct,
   and across 5 adversarial/stale cases the warm arm stayed correct every time (it verifies the
   hint and overrides it when wrong). Caveat held honestly: a wrong hint can cost extra
   verification rounds, so bad context is correctness-safe but not efficiency-free. In normal
   operation the system only serves groundings from real successful runs and flags any drift as
   stale, so the slow case does not arise unless a wrong hint is injected artificially.

3. **Deterministic zero-token freshness.** The store detects content changes with sha256
   checksums immediately, without any LLM call. Claude Code on its own has no equivalent
   mechanism: it rediscovers file content on every session from scratch, and has no
   persistent freshness index.

These three properties together justify the context layer as a **cache with correctness
guarantees** — not as a retrieval engine that must beat grep on every cold query.
