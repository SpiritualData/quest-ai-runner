"""Score one live search case: the search returned no error, an answer phrase appears in the observation the
planner would see (the right passage came back), and no url repeats. ``aggregate`` adds what the run spent."""
import re

from web_search.score import TAG


def score_case(fn, case):
    out = fn(case)
    text = out["text"].lower()
    found = [p for p in case["answers"] if p.lower() in text]
    problems = []
    if out["error"]:
        problems.append(f"error: {out['error']}")
    if not found:
        problems.append(f"no answer phrase {case['answers']} in the observation")
    if len(set(out["urls"])) != len(out["urls"]):
        problems.append("duplicate urls")
    if TAG.search(out["text"]):
        problems.append("html tag in the observation")
    return dict(ok=not problems, backend=out["backend"], calls=out["backend_calls"], tokens=out["est_tokens"],
                latency_ms=out["latency_ms"], got=out["backend"],
                note="ok" if not problems else "; ".join(problems))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))] if ordered else 0.0


def aggregate(rows):
    lat = [r["latency_ms"] for r in rows if "latency_ms" in r]
    calls = sum(r.get("calls", 0) for r in rows)
    backends = sorted({r["backend"] for r in rows if r.get("backend")})
    return (f"answer found {sum(r['ok'] for r in rows)}/{len(rows)} via {','.join(backends) or 'none'}; "
            f"{calls} real backend calls; mean {sum(r.get('tokens', 0) for r in rows) / max(len(rows), 1):.0f} tokens added; "
            f"p50 {percentile(lat, 0.5):.0f} ms, p95 {percentile(lat, 0.95):.0f} ms")
