# World

The AI system optimally learns and interacts with the world outside of it.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `the-world:composition` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Models, search backends and tools change faster than the assistant does, so reaching them through one measured path lets the best one run as they move.

## How the solutions fit together

World is how the assistant learns what is outside it and acts on it. Model Plumbing asks a model through one measured path, where Model Tier Chooser picks the model for each tier and Vendor Model Caller asks that vendor. Tools lets the assistant use the tool of each category the person's authority allows, through Web Search, File Editing and Delegated Agent Sessions. Science answers from public research with its sources.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Model Tier Chooser | `model-tier-chooser` | `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

## Solutions from a library

- [Model Tier Chooser](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/model_plumbing/README.md) (`model-tier-chooser`): Picks, for each model tier, the provider and model that passes the most work per dollar on the same cases, counting prepaid credits as cost. Adopted from the library `hub`.

## Metrics and their leaders

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): leader `qar_selection`, 42 of 42 cases

## Where to go next

- Sub-intent: [Science](science/README.md) (What a request needs from beyond the person's own material comes from the world, with its source.)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records beside the code: [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](model_plumbing/model_tier_chooser/README.md), [quest_ai_runner/the_world/model_plumbing/spend](model_plumbing/spend/README.md), [quest_ai_runner/the_world/model_plumbing/vendor_model_caller](model_plumbing/vendor_model_caller/README.md), [quest_ai_runner/the_world/tools/connected_system_operations](tools/connected_system_operations/README.md), [quest_ai_runner/the_world/tools/delegated_agent_sessions](tools/delegated_agent_sessions/README.md), [quest_ai_runner/the_world/tools/file_editing](tools/file_editing/README.md), [quest_ai_runner/the_world/tools/file_reading](tools/file_reading/README.md), [quest_ai_runner/the_world/tools/web_search](tools/web_search/README.md)
- Records: [intent.csv](intent.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Model Tier Chooser | `model-tier-chooser` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py](model_plumbing/model_tier_chooser/deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py](model_plumbing/model_tier_chooser/model_registry.py) |  |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py](model_plumbing/model_tier_chooser/usage_limit.py) |  |
| Static price table lookup | `model-plumbing:pricing` | [quest_ai_runner/the_world/model_plumbing/spend/pricing.py](model_plumbing/spend/pricing.py) |  |
| Persisted daily token tracker | `model-plumbing:usage` | [quest_ai_runner/the_world/model_plumbing/spend/usage.py](model_plumbing/spend/usage.py) |  |
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py](model_plumbing/vendor_model_caller/providers.py) |  |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py](model_plumbing/vendor_model_caller/claude_cli_provider.py) |  |
| MCP gateway bridge transport | `intake-sources:openclaw` | [quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py](tools/connected_system_operations/openclaw_channel.py) |  |
| Parallel multi-source composite | `quest-context:composite` | [quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py](tools/connected_system_operations/composite_retrieval_adapter.py) |  |
| MCP server reader | `quest-context:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py](tools/connected_system_operations/mcp_retrieval_adapter.py) |  |
| Notion database reader | `quest-context:notion` | [quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py](tools/connected_system_operations/notion_adapter.py) |  |
| Drive document reader | `quest-context:google_drive` | [quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py](tools/connected_system_operations/google_drive_adapter.py) |  |
| Chat conversation reader | `quest-context:google_chat` | [quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py](tools/connected_system_operations/google_chat_adapter.py) |  |
| MCP operation writer | `write-through-mcp:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py](tools/connected_system_operations/mcp_write_adapter.py) |  |
| Agent-protocol deep runner | `delegated-agent-sessions:acp` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py](tools/delegated_agent_sessions/acp_deep_runner.py) |  |
| Persistent stream-json runner | `delegated-agent-sessions:stream_json` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py](tools/delegated_agent_sessions/goal_runner_stream.py) |  |
| Contained backed-up writer | `file-editing:files` | [quest_ai_runner/the_world/tools/file_editing/files_writer.py](tools/file_editing/files_writer.py) |  |
| Local file tree reader | `quest-context:files` | [quest_ai_runner/the_world/tools/file_reading/files_adapter.py](tools/file_reading/files_adapter.py) |  |
| Remote file server reader | `quest-context:remote_files_adapter` | [quest_ai_runner/the_world/tools/file_reading/remote_files_adapter.py](tools/file_reading/remote_files_adapter.py) |  |
| Web research with the answer's support check (today) | `the-science:web_with_support_check` | [quest_ai_runner/the_world/tools/web_search/area.py](tools/web_search/area.py) | [science](science/README.md) |

### Metrics

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): `esoi run choose_a_model_tier --impl qar_selection`
