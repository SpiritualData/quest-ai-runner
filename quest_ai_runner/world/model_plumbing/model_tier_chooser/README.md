# model_tier_chooser

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Model Plumbing](../README.md) > model_tier_chooser

Part of [Model Plumbing](../README.md)

### Top implementations of Model Tier Chooser

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Live selection evaluation arm (`qar_selection`) | 100% (42 of 42 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Model tier selection and limit-recognition correctness. offline cases: deep starting rung per difficulty and ladder (synonyms, unknown rating, off-ladder start, operator mapping), family resolution to the newest live id, tier resolution with and without live models and pins, lower-tier fallback, vision capability, outage wording, usage-limit detection and reset parsing; zero model calls Read in this direction: maximise the share of work started on the cheapest adequate rung; maximise correct resolution of names to live models with operator pins always winning; maximise correct recognition of outages and allowance limits and of their reset time.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Hub tier rule for a deep run | `model-tier-chooser:deep_model_selection` | Starts a deep run on the tier the hub's rule gives (the person's requested start tier, which may still escalate, else standing guidance (a card, then the environment's default work model), else the cheapest tier measured good enough; the environment's allowed models limit every rung), resolved to a model through the registry; an operator's start models count as standing guidance; an escalation moves one rung for one task and needs a specific reason; sub-agents default to the hub's sub-agent tier. | [`quest_ai_runner/world/model_plumbing/model_tier_chooser/deep_model_selection.py`](deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | Maps a tier name to the newest live model id by bucketing a provider's latest-first list by family, caching the result, using a last-known fallback only when the live list is empty, and exposing the next lower tier and vision capability. | [`quest_ai_runner/world/model_plumbing/model_tier_chooser/model_registry.py`](model_registry.py) | selected |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | Detects a subscription usage-limit message in a run's result, parses its reset time and zone, computes a resume time, and records the limit lane-wide with optional persistence so other work waits. | [`quest_ai_runner/world/model_plumbing/model_tier_chooser/usage_limit.py`](usage_limit.py) |  |
| Call roles over the hub's tier table | `model-tier-chooser:call_roles` | Binds each Quest AI Runner model call (a prompt spec id or a direct call) to a role of the hub's role table, which gives the tier, and applies a person's or lane's explicit tier under three guards: classification never above fast, a response never below balanced, best only by a person's explicit tier or the structure-watch role. Deep work defaults to balanced. The legacy word science reads as its role's tier. | [`quest_ai_runner/world/model_plumbing/model_tier_chooser/call_roles.py`](call_roles.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Model Plumbing](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
