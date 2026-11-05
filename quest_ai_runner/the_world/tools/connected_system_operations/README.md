# connected_system_operations

*ESOI records kept beside the code in `quest_ai_runner/the_world/tools/connected_system_operations`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| MCP gateway bridge transport | `talk-over-a-channel:openclaw` | [`quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py`](openclaw_channel.py) |  |
| Parallel multi-source composite | `quest-context:composite` | [`quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py`](composite_retrieval_adapter.py) | selected |
| MCP server reader | `quest-context:mcp` | [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py`](mcp_retrieval_adapter.py) |  |
| Notion database reader | `quest-context:notion` | [`quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py`](notion_adapter.py) |  |
| Drive document reader | `quest-context:google_drive` | [`quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py`](google_drive_adapter.py) |  |
| Chat conversation reader | `quest-context:google_chat` | [`quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py`](google_chat_adapter.py) |  |
| MCP operation writer | `quest-change-executor:mcp` | [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Grounded AI that advances people's goals](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)
