# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Write In Names

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Machine text refused over the outward-text cases. offline cases of texts one outward write would send: the 2026-11-16 subject (a header label and an id), a header on its own line, ids in a body, one id repeated, a persona id, and readable texts including links that carry ids.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Id and machine-header refusal | `write-in-names:readable_guard` | Runs the production check that the outward write tools call on each text a write would send, and reports whether the write is refused and which ids or header labels it named. | [`evaluation/components/write_in_names/impls/readable_guard.py`](readable_guard.py) | selected |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
