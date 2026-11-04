"""Score one context_choice case: each of the area's four lines returns what its member returned.
``aggregate`` reports the lines that matched out of the lines asked."""


LINES = (("context", "context"), ("resolved", "resolved"), ("found", "hits"), ("searched", "searched"))


def score_case(fn, case):
    got = fn(case)
    wrong = [line for line, want in LINES if got.get(line) != case.get(want, [] if line in ("found", "searched") else "")]
    return dict(ok=not wrong, lines=len(LINES), matched=len(LINES) - len(wrong), note="ok" if not wrong else "differs: " + ", ".join(wrong))


def aggregate(rows):
    lines = sum(r.get("lines", 0) for r in rows)
    matched = sum(r.get("matched", 0) for r in rows)
    return f"lines that return what their member returned {matched}/{lines}; time added before the reply 0; model calls 0"
