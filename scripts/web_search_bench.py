#!/usr/bin/env python3
"""Measure WebResearchAdapter.search() latency, result size, and real-call cost.

Runs a set of queries through ``WebResearchAdapter.search()`` (one query per call, so each
query's own latency is visible) and reports per-query latency/size/cache-hit, plus a summary:
p50/p95 latency, mean added tokens (an estimate of how much context each search call costs the
planner), total REAL backend calls (cache hits excluded), and an estimated dollar cost from
``--cost-per-1k``.

The backend is whatever ``build_web_research_from_env()`` picks from the process environment
(see ``docs/web-search.md`` for the env vars) -- this script is generic, consumer-agnostic
tooling, the same as any other script in ``scripts/``.

Input is either a JSON file:

    {
      "search":    [{"id": "q1", "q": "current weather in Portland Oregon"}, ...],
      "no_search": [{"id": "q2", "q": "2 + 2"}, ...]
    }

(``no_search`` entries are counted and reported as skipped, never run -- they're there so a
dataset shared with some other eval that DOES care about the search/no-search split can be
pointed at this tool unmodified) or an ad hoc list via ``--queries``.

Examples:

    python3 scripts/web_search_bench.py --queries "capital of Mongolia" "latest Python version"
    python3 scripts/web_search_bench.py questions.json --parallel 4 --cost-per-1k 1.0
    python3 scripts/web_search_bench.py questions.json --json > report.json

Does nothing on import; makes no network calls until ``main()`` actually runs.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass
class QueryItem:
    id: str
    q: str
    group: str = "search"


@dataclass
class QueryResult:
    id: str
    q: str
    group: str
    latency_ms: float
    num_results: int
    text_chars: int
    est_tokens: int
    cache_hit: bool
    backend_name: str
    error: Optional[str] = None


@dataclass
class BenchReport:
    results: List[QueryResult] = field(default_factory=list)
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    mean_added_tokens: float = 0.0
    total_queries: int = 0
    cache_hits: int = 0
    total_backend_calls: int = 0

    def estimated_cost(self, cost_per_1k: float) -> float:
        return (self.total_backend_calls / 1000.0) * cost_per_1k


# ---------------------------------------------------------------------------
# Loading queries
# ---------------------------------------------------------------------------


def _coerce_items(raw: Any, group: str) -> List[QueryItem]:
    items: List[QueryItem] = []
    for i, entry in enumerate(raw or []):
        if isinstance(entry, str):
            items.append(QueryItem(id=f"{group}-{i}", q=entry, group=group))
        elif isinstance(entry, dict) and entry.get("q"):
            items.append(QueryItem(id=str(entry.get("id") or f"{group}-{i}"), q=str(entry["q"]), group=group))
    return items


def load_queries(
    input_path: Optional[str], ad_hoc: Optional[Sequence[str]]
) -> Tuple[List[QueryItem], List[QueryItem]]:
    """Return ``(run_items, skipped_items)``. ``skipped_items`` are a file's "no_search" entries."""
    if ad_hoc:
        return [QueryItem(id=f"q{i}", q=q, group="search") for i, q in enumerate(ad_hoc)], []

    if not input_path:
        return [], []

    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    run_items = _coerce_items(raw.get("search"), "search")
    skipped_items = _coerce_items(raw.get("no_search"), "no_search")
    return run_items, skipped_items


# ---------------------------------------------------------------------------
# Benchmark core (no CLI / env / import-time side effects -- the offline test calls this directly
# with a fake adapter, exactly the way it would be driven from real code)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo)


def _run_one(adapter: Any, item: QueryItem, *, max_results: Optional[int], fresh: bool, estimate_tokens) -> QueryResult:
    # The public WebResearchAdapter surface has no cache-hit signal, so this diagnostic tool reads
    # the cache's own hit counter around the call -- the cache object is always a WebCache
    # instance (constructed internally when none is passed in), just not exposed as a named
    # public attribute on the adapter.
    cache = getattr(adapter, "_cache", None)
    hits_before = cache.stats()["hits"] if cache is not None else None

    t0 = time.perf_counter()
    obs = adapter.search(item.q, max_results=max_results, fresh=fresh)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    hits_after = cache.stats()["hits"] if cache is not None else None
    cache_hit = bool(hits_before is not None and hits_after is not None and hits_after > hits_before)

    text = obs.text or ""
    return QueryResult(
        id=item.id,
        q=item.q,
        group=item.group,
        latency_ms=latency_ms,
        num_results=len(obs.hits or []),
        text_chars=len(text),
        est_tokens=estimate_tokens(text),
        cache_hit=cache_hit,
        backend_name=getattr(adapter, "backend_name", "unknown"),
        error=obs.error if obs.kind == "error" else None,
    )


def run_benchmark(
    adapter: Any,
    items: Sequence[QueryItem],
    *,
    parallel: int = 1,
    max_results: Optional[int] = None,
    fresh: bool = False,
) -> BenchReport:
    """Run ``adapter.search()`` once per item (sequentially, or across ``parallel`` threads)."""
    from quest_ai_runner.adapters.web_page_extract import estimate_tokens

    if not items:
        return BenchReport()

    if parallel and parallel > 1:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(
                pool.map(lambda it: _run_one(adapter, it, max_results=max_results, fresh=fresh, estimate_tokens=estimate_tokens), items)
            )
    else:
        results = [_run_one(adapter, it, max_results=max_results, fresh=fresh, estimate_tokens=estimate_tokens) for it in items]

    latencies = sorted(r.latency_ms for r in results)
    cache_hits = sum(1 for r in results if r.cache_hit)
    total_backend_calls = len(results) - cache_hits
    mean_added_tokens = sum(r.est_tokens for r in results) / len(results)

    return BenchReport(
        results=results,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        mean_added_tokens=mean_added_tokens,
        total_queries=len(results),
        cache_hits=cache_hits,
        total_backend_calls=total_backend_calls,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: BenchReport, *, skipped_count: int, cost_per_1k: float) -> None:
    for r in report.results:
        status = "ERROR: " + r.error if r.error else "ok"
        cache_note = "cache-hit" if r.cache_hit else "backend-call"
        print(
            f"[{r.group}] {r.id!r} {r.q!r}: {r.latency_ms:.0f}ms, {r.num_results} results, "
            f"{r.text_chars} chars (~{r.est_tokens} tok), {cache_note} via {r.backend_name} -- {status}"
        )
    print("")
    print(f"queries run:          {report.total_queries}")
    if skipped_count:
        print(f"skipped (no_search):   {skipped_count}")
    print(f"p50 latency:           {report.p50_latency_ms:.0f}ms")
    print(f"p95 latency:           {report.p95_latency_ms:.0f}ms")
    print(f"mean added tokens:     {report.mean_added_tokens:.0f}")
    print(f"cache hits:            {report.cache_hits}")
    print(f"real backend calls:    {report.total_backend_calls}")
    if cost_per_1k:
        print(f"estimated cost:        ${report.estimated_cost(cost_per_1k):.4f} (at ${cost_per_1k:.2f}/1k calls)")


def report_to_dict(report: BenchReport, *, skipped_count: int, cost_per_1k: float) -> Dict[str, Any]:
    d = asdict(report)
    d["skipped_count"] = skipped_count
    d["estimated_cost"] = report.estimated_cost(cost_per_1k)
    return d


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    parser.add_argument("input", nargs="?", help="JSON file: {\"search\": [...], \"no_search\": [...]}")
    parser.add_argument("--queries", nargs="+", help="Ad hoc queries instead of a JSON file")
    parser.add_argument("--parallel", type=int, default=1, help="Run up to N queries concurrently (default: sequential)")
    parser.add_argument("--max-results", type=int, default=None, help="Override WEB_SEARCH_MAX_RESULTS for this run")
    parser.add_argument("--fresh", action="store_true", help="Bypass the cache READ for every query (still writes)")
    parser.add_argument("--cost-per-1k", type=float, default=0.0, help="$ per 1,000 real backend calls, for the cost estimate")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON instead of text")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not args.input and not args.queries:
        print("error: provide a JSON file or --queries", file=sys.stderr)
        return 2

    run_items, skipped_items = load_queries(args.input, args.queries)
    if not run_items:
        print("error: no queries to run (file had none under \"search\", or --queries was empty)", file=sys.stderr)
        return 2

    from quest_ai_runner.adapters.web_research import build_web_research_from_env

    adapter = build_web_research_from_env()
    if adapter is None:
        print(
            "error: no web search backend configured. Set one of SERPER_API_KEY, "
            "BRAVE_SEARCH_API_KEY/BRAVE_API_KEY, TAVILY_API_KEY, SEARXNG_URL, or "
            "GEMINI_API_KEY/GOOGLE_API_KEY/GOOGLE_AI_API_KEY (and WEB_SEARCH_ENABLED != false).",
            file=sys.stderr,
        )
        return 1

    report = run_benchmark(adapter, run_items, parallel=args.parallel, max_results=args.max_results, fresh=args.fresh)

    if args.json:
        print(json.dumps(report_to_dict(report, skipped_count=len(skipped_items), cost_per_1k=args.cost_per_1k), indent=2))
    else:
        print_report(report, skipped_count=len(skipped_items), cost_per_1k=args.cost_per_1k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
