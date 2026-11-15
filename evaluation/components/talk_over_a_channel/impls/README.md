# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Intake Sources

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Fake-gateway conformance arm (`openclaw_bridge`) | 100% (20 of 20 cases) | free |  |
| 2 | `real_gateway` | 30% (6 of 20 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Channel transport conformance to the gateway wire contract. offline cases with a fake MCP gateway: ordered inbound messages, key-spelling drift, non-message and malformed events, wire failure and dead gateway, long-poll clamp, attachments, send args and ids, allowed tools only, close The payload shapes are tolerant guesses (no live gateway was available), so the result is conformance to those shapes, and there is only one channel.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Fake-gateway conformance arm | `intake-sources:openclaw_bridge` | Drives the production channel with a fake MCP client injected at the process edge and records ids, texts, health, poll timeout, attachment sizes and whether only allowed tools were called. | [`evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py`](openclaw_bridge.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
