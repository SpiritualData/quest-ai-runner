# web_search

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Tools](../README.md) > web_search

Part of [Tools](../README.md)

### Top implementations of Web Search

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `web_research` | 100% (10 of 10 cases) | $0.0350 |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Right-passage rate and search spend over public factual questions. Live run of scripts/web_search_bench.py through the production adapter built from the environment (evaluation/components/web_search_live): an answer phrase in the observation the planner would see over public factual questions, real backend calls, latency and tokens added, with search spend at the list price per call.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Claude Code login search | `web-search:claude_login` | The hub's keyless search through the Claude Code login: one headless Claude call allowed only the CLI's own web search tool. Chosen by name (QAR_WEB_SEARCH_BACKEND=claude_login) or by the hub loop when the lane has no search backend. | [`quest_ai_runner/world/tools/web_search/web_search_backends.py`](web_search_backends.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
