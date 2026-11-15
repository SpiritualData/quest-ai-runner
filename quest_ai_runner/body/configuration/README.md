# configuration

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Body](../README.md) > configuration

Part of [Quest Body](../README.md)

### Top implementations of Quest Context

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Parallel multi-source composite (`composite`) | 90% (19 of 21 cases) | free | selected |
| 2 | `first_three` | 0% (0 of 21 cases) | n/a |  |

**How it is scored:** Retrieval recall of the right document over handbook questions. Complete retrieval stacks only: recall@3 of the right document, and its answer readable, over questions on a synthetic handbook (live adapters, fake network edge only); the composite stack is ranked against its floor.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Registry of built-in and host resolvers | `quest-context:note` | A small protocol plus a registry mapping item type to resolver, with built-in note and file resolvers, coercion of plain callables into resolvers, and host entries overriding built-ins. Missing types render an unresolved-pointer line and errors return empty text. | [`quest_ai_runner/body/configuration/config_assembler.py`](config_assembler.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Body](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
