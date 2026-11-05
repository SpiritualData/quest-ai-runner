# Science

What a request needs from beyond the person's own material comes from the world, with its source.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `the-science:web_with_support_check` of its parent intent [`the_world`](../README.md). Opportunities: Facts about the world arrive with a source and a strength the person can weigh, which the parent solution leaves aside: a low search spend per correct answer through snippet-first research, a check that a claim is supported by the page it cites, and how an answer is weighed when sources disagree or the model's own memory is the only source.

## How the solutions fit together

Science answers what lies beyond a person's own material from public research, with the source shown. Web Search brings in the live web when the research sources do not answer.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Web Search | `web-search` | `quest_ai_runner.run("web-search", 'latest Rust release')` | Snippet-first research with page extraction (`web_research`) | Right-passage rate and search spend over public factual questions |

## Solutions from a library

- [Web Search](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`web-search`): One narrow call: a query in, a response of hits out. Adopted from the library `hub`.

## Metrics and their leaders

- Right-passage rate and search spend over public factual questions (`metric-search-the-web`): leader `web_research`, 10 of 10 cases

## Where to go next

- Parent intent: [World](../README.md)
- Repository: [top README](../../../README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Web Search | `web-search` | Snippet-first research with page extraction (`web_research`) | Right-passage rate and search spend over public factual questions |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Snippet-first research with page extraction | `web-search:web_research` | [quest_ai_runner/the_world/tools/web_search/web_research.py](../tools/web_search/web_research.py) |  |
| Model-native search | `web-search:provider_web_search` | [quest_ai_runner/the_world/tools/web_search/provider_web_search_adapter.py](../tools/web_search/provider_web_search_adapter.py) |  |
| Pluggable search backends with fallback | `web-search:serper` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |
| Brave search backend | `web-search:brave` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |
| Gemini grounded search backend | `web-search:gemini` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |
| Provider-native search backend | `web-search:provider` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |
| SearXNG search backend | `web-search:searxng` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |
| Tavily search backend | `web-search:tavily` | [quest_ai_runner/the_world/tools/web_search/web_search_backends.py](../tools/web_search/web_search_backends.py) |  |

### Metrics

- Right-passage rate and search spend over public factual questions (`metric-search-the-web`): `esoi run web_search_live --impl web_research; esoi run web_search --impl fallback_chain`
