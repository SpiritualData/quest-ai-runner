# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

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
| Creator keeps the goal, no model call (floor, proposed) | `quest-representatives:creator_keeps_it` | Proposed. A new goal stays with its creator unless the person assigns it. | [`evaluation/components/hand_off_a_goal/impls/creator_keeps_it.py`](creator_keeps_it.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
