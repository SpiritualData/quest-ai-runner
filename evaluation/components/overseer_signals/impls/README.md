# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| No-judgement digest probe | `nervous-system:digest_probe` | A floor arm that always returns no signal, so the scorer measures only whether the digest shows the evidence each label turns on. | [`evaluation/components/overseer_signals/impls/digest_probe.py`](digest_probe.py) |  |
| Live overseer model arm | `nervous-system:live_model` | Calls the production overseer function against the real provider, one small call per scenario, and fails if the overseer degraded. | [`evaluation/components/overseer_signals/impls/live_model.py`](live_model.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
