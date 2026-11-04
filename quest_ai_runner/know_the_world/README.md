# Knowing the world with its source

What a request needs to know beyond the person's own material comes from the world with its source, so the person can weigh it.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `know-the-world:web_with_support_check` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Facts about the world arrive with a source and a strength the person can weigh, which the parent solution leaves aside: a low search spend per correct answer through snippet-first research, a check that a claim is supported by the page it cites, and how an answer is weighed when sources disagree or the model's own memory is the only source.

## How the solutions fit together

Science gives a request the facts it needs from outside the person's own material, from the results of Bounded web research, which it checks for support and returns with their source.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Bounded web research | `search-the-web` | `quest_ai_runner.run("search-the-web", 'latest Rust release')` | Snippet-first research with page extraction (`web_research`) | Right-passage rate and search spend over public factual questions |

## Problems and ideals

**Problems**

- Answering from the live web risks fetching addresses that should never be fetched, paying again for the same search, or returning the wrong passage from the right page. (`prob-web-answers-unsafe-or-costly`)

**Ideals**

- Safe Search (`ideal-safe-search`): Web answers come from the right passage, fetched safely and paid for once.

## Metrics and their leaders

- Right-passage rate and search spend over public factual questions (`metric-search-the-web`): leader `web_research`, 10 of 10 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Answering from the live web risks fetching addresses that should never be fetched, paying again for the same search, or returning the wrong passage from the right page. (`prob-web-answers-unsafe-or-costly`)

### Ideals

- Safe Search (`ideal-safe-search`): Web answers come from the right passage, fetched safely and paid for once.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Bounded web research | `search-the-web` | Snippet-first research with page extraction (`web_research`) | Right-passage rate and search spend over public factual questions |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Snippet-first research with page extraction | `search-the-web:web_research` | [quest_ai_runner/know_the_world/web_research.py](web_research.py) |  |
| Model-native search | `search-the-web:provider_web_search` | [quest_ai_runner/know_the_world/provider_web_search_adapter.py](provider_web_search_adapter.py) |  |
| Pluggable search backends with fallback | `search-the-web:serper` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |
| Brave search backend | `search-the-web:brave` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |
| Gemini grounded search backend | `search-the-web:gemini` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |
| Provider-native search backend | `search-the-web:provider` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |
| SearXNG search backend | `search-the-web:searxng` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |
| Tavily search backend | `search-the-web:tavily` | [quest_ai_runner/know_the_world/web_search_backends.py](web_search_backends.py) |  |

### Metrics

- Right-passage rate and search spend over public factual questions (`metric-search-the-web`): `esoi run web_search_live --impl web_research; esoi run web_search --impl fallback_chain`
