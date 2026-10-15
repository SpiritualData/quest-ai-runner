"""The production auto chain: ``FallbackSearchBackend`` over serper, brave, tavily and searxng in that order
(what ``select_search_backend`` builds when several keys are configured). A backend that errors or returns
nothing hands the query to the next; a 429 puts that backend in cooldown. Zero model calls, zero network."""
from web_search import bench
from quest_ai_runner.adapters.web_search_backends import FallbackSearchBackend
from web_search.impls._vendor import backend_for

ORDER = ("serper", "brave", "tavily", "searxng")


def build(meter):  # noqa: ARG001
    def search(case):
        chain = FallbackSearchBackend([backend_for(v, case["payloads"]) for v in ORDER])
        return bench.run(chain, case)
    search.impl = "fallback_chain"
    return search
