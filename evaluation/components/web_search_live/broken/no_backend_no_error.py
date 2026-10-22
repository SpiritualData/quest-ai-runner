"""BASELINE: the search function a first draft writes when the backend call is stubbed out and its failure is
swallowed. It never reaches a search engine, hands the planner the query itself as the observation and reports
no error, so the run looks successful while no passage, and no answer, came back. Zero backend calls, zero spend."""


def build(meter):  # noqa: ARG001
    def search(case):
        text = f"Search results for: {case['query']}"
        return dict(text=text, urls=[], error="", backend="none", backend_calls=0,
                    est_tokens=len(text) // 4, latency_ms=0.0)
    return search
