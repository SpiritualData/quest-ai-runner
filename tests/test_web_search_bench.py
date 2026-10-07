"""Tests for scripts/web_search_bench.py -- offline, against a fake WebResearchAdapter backend.

No network: everything runs through a scripted fake SearchBackend, the same pattern
tests/test_web_research.py uses.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from quest_ai_runner.adapters.web_cache import WebCache
from quest_ai_runner.adapters.web_research import WebResearchAdapter
from quest_ai_runner.adapters.web_search_backends import SearchHit, SearchResponse

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "web_search_bench.py"


def load_tool():
    """Import the script by path (it lives in scripts/, which is not a package)."""
    spec = importlib.util.spec_from_file_location("web_search_bench_tool", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses' type-hint resolution looks the module up in sys.modules by __module__ name;
    # register it before exec_module so that lookup doesn't hit None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


class FakeBackend:
    name = "fake-bench-backend"

    def __init__(self):
        self.calls = []

    def search(self, query, *, max_results=5):
        self.calls.append(query)
        return SearchResponse(
            hits=[SearchHit(title=f"Result for {query}", url=f"https://example.com/{query}", snippet="A snippet.")],
            answer="",
        )


def test_import_has_no_side_effects():
    # Importing already happened at module load above; nothing should have touched the network
    # or printed anything. If import had side effects this test module itself would already be
    # broken, so just assert the expected symbols exist.
    assert hasattr(tool, "run_benchmark")
    assert hasattr(tool, "main")


def test_run_benchmark_against_fake_backend_reports_latency_and_size():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    items = [tool.QueryItem(id="q1", q="capital of Mongolia", group="search")]

    report = tool.run_benchmark(adapter, items)

    assert report.total_queries == 1
    assert report.cache_hits == 0
    assert report.total_backend_calls == 1
    r = report.results[0]
    assert r.id == "q1"
    assert r.num_results == 1
    assert r.text_chars > 0
    assert r.est_tokens > 0
    assert r.cache_hit is False
    assert r.backend_name == "fake-bench-backend"
    assert r.error is None


def test_run_benchmark_second_identical_query_is_a_cache_hit():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    items = [
        tool.QueryItem(id="q1", q="same query", group="search"),
        tool.QueryItem(id="q2", q="same query", group="search"),
    ]

    report = tool.run_benchmark(adapter, items)

    assert report.total_queries == 2
    assert report.cache_hits == 1
    assert report.total_backend_calls == 1
    assert backend.calls == ["same query"]  # backend only actually called once
    assert report.results[1].cache_hit is True


def test_run_benchmark_fresh_bypasses_cache_for_every_query():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    items = [tool.QueryItem(id="q1", q="same query"), tool.QueryItem(id="q2", q="same query")]

    report = tool.run_benchmark(adapter, items, fresh=True)

    assert report.cache_hits == 0
    assert report.total_backend_calls == 2
    assert backend.calls == ["same query", "same query"]


def test_run_benchmark_parallel_runs_all_queries():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    items = [tool.QueryItem(id=f"q{i}", q=f"query {i}") for i in range(5)]

    report = tool.run_benchmark(adapter, items, parallel=4)

    assert report.total_queries == 5
    assert report.total_backend_calls == 5
    assert sorted(backend.calls) == [f"query {i}" for i in range(5)]


def test_run_benchmark_reports_percentiles_and_mean_tokens():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    items = [tool.QueryItem(id=f"q{i}", q=f"query {i}") for i in range(4)]

    report = tool.run_benchmark(adapter, items)

    assert report.p50_latency_ms >= 0.0
    assert report.p95_latency_ms >= report.p50_latency_ms
    assert report.mean_added_tokens > 0


def test_run_benchmark_empty_items_returns_empty_report():
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())
    report = tool.run_benchmark(adapter, [])
    assert report.total_queries == 0
    assert report.results == []


def test_estimated_cost():
    report = tool.BenchReport(total_backend_calls=500)
    assert report.estimated_cost(cost_per_1k=2.0) == pytest.approx(1.0)
    assert report.estimated_cost(cost_per_1k=0.0) == 0.0


# ---------------------------------------------------------------------------
# load_queries: JSON file with search/no_search, and ad hoc --queries
# ---------------------------------------------------------------------------


def test_load_queries_from_ad_hoc_list():
    run_items, skipped = tool.load_queries(None, ["q one", "q two"])
    assert [i.q for i in run_items] == ["q one", "q two"]
    assert skipped == []


def test_load_queries_from_json_file_splits_search_and_no_search(tmp_path):
    data = {
        "search": [{"id": "s1", "q": "real search question"}],
        "no_search": [{"id": "n1", "q": "2 + 2"}],
    }
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    run_items, skipped = tool.load_queries(str(path), None)
    assert len(run_items) == 1
    assert run_items[0].id == "s1"
    assert len(skipped) == 1
    assert skipped[0].id == "n1"


def test_load_queries_no_input_returns_nothing():
    run_items, skipped = tool.load_queries(None, None)
    assert run_items == []
    assert skipped == []


# ---------------------------------------------------------------------------
# CLI end to end (main()), with build_web_research_from_env monkeypatched to the fake backend
# so the whole CLI path runs with zero network access.
# ---------------------------------------------------------------------------


def test_main_end_to_end_against_fake_backend(monkeypatch, capsys):
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())

    import quest_ai_runner.adapters.web_research as web_research_module

    monkeypatch.setattr(web_research_module, "build_web_research_from_env", lambda *a, **k: adapter)

    exit_code = tool.main(["--queries", "capital of Mongolia", "latest python version"])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "capital of Mongolia" in out
    assert "queries run:" in out
    assert "p50 latency:" in out
    assert "real backend calls:" in out


def test_main_json_output(monkeypatch, capsys):
    backend = FakeBackend()
    adapter = WebResearchAdapter(backend, cache=WebCache())

    import quest_ai_runner.adapters.web_research as web_research_module

    monkeypatch.setattr(web_research_module, "build_web_research_from_env", lambda *a, **k: adapter)

    exit_code = tool.main(["--queries", "a question", "--json"])
    assert exit_code == 0

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["total_queries"] == 1
    assert "results" in parsed


def test_main_no_backend_configured_returns_error(monkeypatch, capsys):
    import quest_ai_runner.adapters.web_research as web_research_module

    monkeypatch.setattr(web_research_module, "build_web_research_from_env", lambda *a, **k: None)

    exit_code = tool.main(["--queries", "a question"])
    assert exit_code == 1
    assert "no web search backend configured" in capsys.readouterr().err


def test_main_no_queries_and_no_input_is_an_error(capsys):
    exit_code = tool.main([])
    assert exit_code == 2
    assert "provide a JSON file or --queries" in capsys.readouterr().err
