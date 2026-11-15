# Write Through MCP

**Purpose:** Carry out one named change on an external tool server, and only when an explicit allowlist permits that operation.

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Tools](../README.md) > Write Through MCP

Part of [Tools](../README.md)

## Write Through MCP (`write-through-mcp`)

- Out of scope: Reads from a tool server (quest-context does that), any operation that is not on the write allowlist, and deciding which quest change a request asks for (quest-change-executor does that).
- Delegates to: quest-context, quest-change-executor
- Entry call: `quest_ai_runner.run("write-through-mcp", ...)`
- Selected implementation: MCP operation writer (`mcp`)
- Metric: Allowlist enforcement and exactly-once execution
- Implemented by: [`quest_ai_runner/world/tools/__init__.py`](../__init__.py), [`quest_ai_runner/world/tools/connected_system_operations/__init__.py`](__init__.py), [`quest_ai_runner/world/tools/connected_system_operations/composite_retrieval_adapter.py`](composite_retrieval_adapter.py), [`quest_ai_runner/world/tools/connected_system_operations/google_chat_adapter.py`](google_chat_adapter.py), [`quest_ai_runner/world/tools/connected_system_operations/google_drive_adapter.py`](google_drive_adapter.py), [`quest_ai_runner/world/tools/connected_system_operations/interface.py`](interface.py), [`quest_ai_runner/world/tools/connected_system_operations/mcp_retrieval_adapter.py`](mcp_retrieval_adapter.py), [`quest_ai_runner/world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py) and 7 more

### Top implementations of Write Through MCP

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `allowlist_writer` | 100% (15 of 15 cases) | free |  |
| 2 | `real_stdio_server` | 47% (7 of 15 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Allowlist enforcement and exactly-once execution. offline cases with a fake MCP client: exact tool and args once, aliased and bare names, write allowlist refusals with zero server calls, server error and dead connection, discovery Real MCP servers are not exercised; the fake is at the client seam.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| MCP operation writer | `write-through-mcp:mcp` | Lists the allow-listed subset of a server's tools as writable operations and executes one by name through the client, refusing anything off the allowlist before the network, with alias namespacing and an audit detail on every result. | [`quest_ai_runner/world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
