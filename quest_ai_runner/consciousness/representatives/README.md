# representatives

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > representatives

Part of [Quest Consciousness](../README.md)

### Top implementations of Quest Requests

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Offline lifecycle harness (`qar_runner`) | 100% (43 of 43 cases) | free | selected |

**How it is scored:** Task lifecycle correctness over offline cases. offline cases: poll, claim, run and report lifecycle (done, failed, needs-you with a decision, duplicate scan, lost claim, scheduled-not-yet-due, team scoping, unconfigured), persona selection by exact structured field, quest-folder pull and push (idempotent, human text kept, bullets pushed once), folder ownership, local-time scheduling, env-file and flag handling; fake Quest client and scripted model only, zero model calls The edge is a fake Quest client and a scripted model, so wire-level behaviour (HTTP errors, real Quest payload shapes) is not scored; the quest_client tests and the dev-Quest qualitative run pin it.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Quest Representatives

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Persona resolver precedence for queued tasks (today) (`persona_resolver_precedence`) | 88% (14 of 16 cases) | free | selected |
| 2 | `first_registered` | 25% (4 of 16 cases) | free |  |

**How it is scored:** Representative choice accuracy and thread consistency. Measures whether each request is handled by the representative that fits it and whether a thread keeps one representative.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Configured persona resolver | `quest-requests:personas` | Chooses who runs a task from configuration, in order: a structured assignment field matching the registry, an optional model-judged explicit ask, then domain dominance, with optional auto-registration of unknown ids. | [`quest_ai_runner/consciousness/representatives/personas.py`](personas.py) |  |
| Persona resolver precedence for queued tasks (today) | `quest-representatives:persona_resolver_precedence` | A structured assignment field first, then a model-judged explicit ask, then domain-card dominance with names excluded, then a structural fallback; a bare mention never selects. | [`quest_ai_runner/consciousness/representatives/personas.py`](personas.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
