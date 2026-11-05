# connected_system_operations

*ESOI records kept beside the code in `quest_ai_runner/the_world/tools/connected_system_operations`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| MCP gateway bridge transport | `talk-over-a-channel:openclaw` | [`quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py`](openclaw_channel.py) | selected |
| Parallel multi-source composite | `gather-evidence:composite` | [`quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py`](composite_retrieval_adapter.py) | selected |
| MCP server reader | `gather-evidence:mcp` | [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py`](mcp_retrieval_adapter.py) |  |
| Notion database reader | `gather-evidence:notion` | [`quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py`](notion_adapter.py) |  |
| Drive document reader | `gather-evidence:google_drive` | [`quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py`](google_drive_adapter.py) |  |
| Chat conversation reader | `gather-evidence:google_chat` | [`quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py`](google_chat_adapter.py) |  |
| MCP operation writer | `write-through-mcp:mcp` | [`quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py`](mcp_write_adapter.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Grounded AI that advances people's goals](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)
