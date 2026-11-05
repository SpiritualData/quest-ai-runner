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
- Implemented by: [`quest_ai_runner/the_world/tools/__init__.py`](../__init__.py), [`quest_ai_runner/the_world/tools/connected_system_operations/__init__.py`](__init__.py), [`quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py`](composite_retrieval_adapter.py), [`quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py`](google_chat_adapter.py), [`quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py`](google_drive_adapter.py), [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py`](mcp_retrieval_adapter.py), [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py), [`quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py`](notion_adapter.py) and 4 more

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| MCP operation writer | `write-through-mcp:mcp` | Lists the allow-listed subset of a server's tools as writable operations and executes one by name through the client, refusing anything off the allowlist before the network, with alias namespacing and an audit detail on every result. | [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
