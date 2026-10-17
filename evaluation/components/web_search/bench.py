"""Shared plumbing for the web-search solutions: the fake HTTP seam, the vendor payload lookup and the run of
``scripts/web_search_bench.py`` (loaded by path, as tests/test_web_search_bench.py does) over one backend."""
import importlib.util
import sys
from pathlib import Path

import common
from quest_ai_runner.adapters.web_cache import WebCache
from quest_ai_runner.adapters.web_research import WebResearchAdapter

_TOOL = None


def tool():
    """The operator bench script, imported once. It does nothing on import."""
    global _TOOL
    if _TOOL is None:
        path = common.REPO / "scripts" / "web_search_bench.py"
        spec = importlib.util.spec_from_file_location("web_search_bench_tool", path)
        _TOOL = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _TOOL        # dataclasses resolve type hints through sys.modules
        spec.loader.exec_module(_TOOL)
    return _TOOL


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


HOSTS = {"google.serper.dev": "serper", "api.search.brave.com": "brave", "api.tavily.com": "tavily",
         "searx.example.org": "searxng"}


def fake_http(payloads):
    """An ``http(method, url, **kwargs)`` that answers from the case's payloads by vendor host."""
    def http(method, url, **kwargs):  # noqa: ARG001
        vendor = next(v for h, v in HOSTS.items() if h in url)
        spec = payloads[vendor]
        return Response(spec.get("status", 200), spec.get("json", {}))
    return http


def run(backend, case):
    """Run the case's query through a fresh adapter (over ``backend``) and the bench; return the result dict."""
    return run_adapter(WebResearchAdapter(backend, cache=WebCache()), case)


def run_adapter(adapter, case, fresh=False):
    """Run the case's query through ``adapter`` and the bench; return the solution's result dict. ``fresh``
    bypasses the adapter's cache READ, so a live run pays for (and measures) a real backend call."""
    t = tool()
    items = [t.QueryItem(id="q1", q=case["query"])] + ([t.QueryItem(id="q2", q=case["query"])] if case.get("repeat") else [])
    max_results = case.get("max_results")
    seen, original = [], adapter.search

    def recording_search(*a, **k):                  # keep what the planner would have seen, once
        seen.append(original(*a, **k))
        return seen[-1]
    adapter.search = recording_search
    report = t.run_benchmark(adapter, items, max_results=max_results, fresh=fresh)
    obs = seen[0]
    first = report.results[0]
    return dict(urls=[h["url"] for h in (obs.hits or [])], text=obs.text or "",
                error=obs.error if obs.kind == "error" else None, backend=first.backend_name,
                est_tokens=first.est_tokens, text_chars=first.text_chars,
                cache_hits=report.cache_hits, backend_calls=report.total_backend_calls,
                latency_ms=first.latency_ms)
