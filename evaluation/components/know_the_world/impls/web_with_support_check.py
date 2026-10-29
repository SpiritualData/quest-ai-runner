"""CURRENT, live: the production web research (``WebResearchAdapter`` over a vendor backend serving the fixture pages) and the
rule the answer follows: state what a retrieved page says, cite it inline, and when no retrieved page covers the question say
that no source was found instead of stating it. The composer is a rule, not a model, so this measures the research-to-citation
path and not the wording of a real model's answer. Zero model calls."""
import common
from know_the_world import compose
from quest_ai_runner.adapters.web_research import WebResearchAdapter
from quest_ai_runner.adapters.web_cache import WebCache
from web_search import bench
from web_search.impls import _vendor


def build(meter):  # noqa: ARG001
    pages = {c["id"]: c for c in common.load_cases("web_search")}

    def answer(case):
        source = dict(pages[case["source"]], query=case["question"])
        found = bench.run_adapter(WebResearchAdapter(_vendor.backend_for("serper", source["payloads"]), cache=WebCache()), source)
        retrieved = found["urls"]
        hit = compose.best_entry(case["question"], found["text"])
        if found["error"] or hit is None:
            return {"text": "I could not find a source that answers this, so I am not stating it.", "cited": [], "retrieved": retrieved}
        return {"text": f"{hit['snippet']} [{hit['title']}]({hit['url']})", "cited": [hit["url"]], "retrieved": retrieved}
    return answer
