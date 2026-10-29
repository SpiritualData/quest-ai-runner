"""Score one world question by what the answer cites, never by a judge.

A supported case passes when the answer states the labelled claim, cites the page that holds it, and cites nothing the research
did not retrieve. An unsupported case (no retrieved page holds the claim, or the search failed) passes when the answer states
none of the claims no page supports, cites nothing, and says it found no source. ``aggregate`` reports the share of claims
that are cited to a retrieved source and the count of claims stated without one."""
import re

CITE = re.compile(r"\]\((https?://[^)\s]+)\)")
NO_SOURCE = ("no source", "could not find", "did not find", "cannot confirm")


def score_case(fn, case):
    got = fn(case)
    text, cited, retrieved = got["text"], got["cited"], set(got["retrieved"])
    problems = []
    if any(url not in retrieved for url in cited):
        problems.append(f"cites a page the research did not retrieve: {[u for u in cited if u not in retrieved]}")
    if case["supported"]:
        if case["claim"].lower() not in text.lower():
            problems.append(f"does not state the claim {case['claim']!r}")
        if case["gold"] not in cited:
            problems.append(f"does not cite {case['gold']}")
    else:
        if case["claim"].lower() in text.lower():
            problems.append(f"states {case['claim']!r}, which no retrieved page holds")
        if cited:
            problems.append("cites a source for a claim no page supports")
        if not any(p in text.lower() for p in NO_SOURCE):
            problems.append("does not say it found no source")
    uncited = 0 if case["supported"] and case["gold"] in cited else 1 if case["claim"].lower() in text.lower() else 0
    return dict(ok=not problems, uncited=uncited, supported=case["supported"], got=("cites " + ",".join(cited)) if cited else "no citation",
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    sup = [r for r in rows if r.get("supported")]
    return (f"{sum(1 for r in sup if r.get('ok'))}/{len(sup)} supported claims cited to a retrieved page; "
            f"{sum(1 for r in rows if not r.get('supported') and r.get('ok'))}/{len(rows) - len(sup)} unsupported claims withheld; "
            f"{sum(r.get('uncited', 0) for r in rows)} claims stated without a source")
