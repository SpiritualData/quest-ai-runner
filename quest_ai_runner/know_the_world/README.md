# Knowing the world with its source

What a request needs to know beyond the person's own material comes from the world with its source, so the person can weigh it.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `know-the-world:web_with_support_check` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Facts about the world arrive with a source and a strength the person can weigh, which the parent solution leaves aside: a low search spend per correct answer through snippet-first research, a check that a claim is supported by the page it cites, and how an answer is weighed when sources disagree or the model's own memory is the only source.

## How the solutions fit together

know-the-world gives a request the facts it needs from outside the person's own material, from the results of search-the-web, which it checks for support and returns with their source.

## Solutions at a glance

| Solution | Entry call | Selected implementation | Metric |
|---|---|---|---|
| `search-the-web` | `quest_ai_runner.run("search-the-web", 'latest Rust release')` | `web_research` | Right-passage rate and search spend over public factual questions |

## Problems and ideals

**Problems**

- `prob-web-answers-unsafe-or-costly`: Answering from the live web risks fetching addresses that should never be fetched, paying again for the same search, or returning the wrong passage from the right page.

**Ideals**

- `ideal-safe-search`, Safe Search: Web answers come from the right passage, fetched safely and paid for once.

## Metrics and their leaders

- `metric-search-the-web`, Right-passage rate and search spend over public factual questions: leader `web_research`, 10 of 10 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- `prob-web-answers-unsafe-or-costly`: Answering from the live web risks fetching addresses that should never be fetched, paying again for the same search, or returning the wrong passage from the right page.

### Ideals

- `ideal-safe-search`, Safe Search: Web answers come from the right passage, fetched safely and paid for once.

### Solutions

| Solution | Name | Selected | Metric |
|---|---|---|---|
| `search-the-web` | Bounded web research | web_research | Right-passage rate and search spend over public factual questions |

### Implementations

| Implementation | Name | Where | Sub-intent |
|---|---|---|---|
| `search-the-web:web_research` | Snippet-first research with page extraction | [quest_ai_runner/adapters/web_research.py](../adapters/web_research.py) |  |
| `search-the-web:provider_web_search` | Model-native search | [quest_ai_runner/adapters/provider_web_search_adapter.py](../adapters/provider_web_search_adapter.py) |  |
| `search-the-web:serper` | Pluggable search backends with fallback | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |
| `search-the-web:brave` | Brave search backend | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |
| `search-the-web:gemini` | Gemini grounded search backend | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |
| `search-the-web:provider` | Provider-native search backend | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |
| `search-the-web:searxng` | SearXNG search backend | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |
| `search-the-web:tavily` | Tavily search backend | [quest_ai_runner/adapters/web_search_backends.py](../adapters/web_search_backends.py) |  |

### Metrics

- `metric-search-the-web`, Right-passage rate and search spend over public factual questions: `esoi run web_search_live --impl web_research; esoi run web_search --impl fallback_chain`
