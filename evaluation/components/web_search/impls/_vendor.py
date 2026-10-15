"""Builds one vendor backend over the fixture HTTP seam. Private helper for the single-vendor solutions."""
from web_search import bench
from quest_ai_runner.adapters import web_search_backends as wsb


def backend_for(vendor, payloads):
    http = bench.fake_http(payloads)
    if vendor == "serper":
        return wsb.SerperBackend("key", http=http)
    if vendor == "brave":
        return wsb.BraveBackend("key", http=http)
    if vendor == "tavily":
        return wsb.TavilyBackend("key", http=http)
    if vendor == "searxng":
        return wsb.SearxngBackend("https://searx.example.org", http=http)
    raise ValueError(vendor)


def single(vendor):
    def build(meter):  # noqa: ARG001
        def search(case):
            return bench.run(backend_for(vendor, case["payloads"]), case)
        search.impl = vendor
        return search
    return build
