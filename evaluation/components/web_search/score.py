"""Score one web-search case. ``expect`` keys (all optional):
  gold            [url, k]   the labelled page must be among the first k urls
  max_urls        int        at most this many urls
  unique_urls     true       no url repeats
  no_tags         true       the observation text holds no HTML tag
  summary         bool       "Summary:" is in the text iff true (vendors that return no summary may differ: see
                             ``summary_vendors`` in the case, a list of the impls expected to carry one)
  error           true       the search must come back as an error (any text)
  error_contains  str        ... and its text must contain this
  cache_hit       true       a repeated query is served from the cache: 1 backend call for 2 searches
A case may carry ``expect_by_impl`` {impl: expect} to state a different expectation for one implementation
(for example a failover case that only the fallback chain can answer)."""
import re

TAG = re.compile(r"</?(?:strong|b|em|i|span|p|br|div|a)\b[^>]*>", re.I)   # real markup; the observation itself prints "<url>"


def _expect_for(case, fn):
    return (case.get("expect_by_impl") or {}).get(getattr(fn, "impl", ""), case["expect"])


def score_case(fn, case):
    out = fn(case)
    exp = _expect_for(case, fn)
    problems = []
    urls = out["urls"]
    if "gold" in exp:
        url, k = exp["gold"]
        if url not in urls[:k]:
            problems.append(f"gold {url} not in top {k}: {urls[:k]}")
    if "max_urls" in exp and len(urls) > exp["max_urls"]:
        problems.append(f"{len(urls)} urls, cap {exp['max_urls']}")
    if exp.get("unique_urls") and len(set(urls)) != len(urls):
        problems.append(f"duplicate urls {urls}")
    if exp.get("no_tags") and TAG.search(out["text"]):
        problems.append("html tag in the observation")
    if "summary" in exp:
        wants = exp["summary"] and (not case.get("summary_vendors") or fn.impl in case["summary_vendors"]
                                    or fn.impl == "fallback_chain")
        if wants and "Summary:" not in out["text"]:
            problems.append("vendor summary missing")
        if not exp["summary"] and "Summary:" in out["text"]:
            problems.append("unexpected summary")
    if exp.get("error") and not out["error"]:
        problems.append("expected an error, got results")
    if exp.get("error") is False and out["error"]:
        problems.append(f"unexpected error: {out['error']}")
    if "error_contains" in exp and exp["error_contains"] not in (out["error"] or ""):
        problems.append(f"error lacks {exp['error_contains']!r}: {out['error']}")
    if exp.get("cache_hit") and not (out["cache_hits"] == 1 and out["backend_calls"] == 1):
        problems.append(f"cache: {out['cache_hits']} hits, {out['backend_calls']} backend calls for 2 searches")
    return dict(ok=not problems, got=f"{len(urls)} urls, ~{out['est_tokens']} tok",
                est_tokens=out["est_tokens"], latency_ms=out["latency_ms"], err=bool(out["error"]),
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    ran = [r for r in rows if not r.get("err")]
    toks = sum(r["est_tokens"] for r in ran) / len(ran) if ran else 0
    lat = sorted(r["latency_ms"] for r in ran)
    p50 = lat[len(lat) // 2] if lat else 0
    return f"{len(ran)} searches answered, {len(rows) - len(ran)} errored, mean ~{toks:.0f} tokens added to the planner, p50 {p50:.1f} ms (fixtures)"
