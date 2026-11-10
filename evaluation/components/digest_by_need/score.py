"""Score one digest_by_need case. ``aggregate`` reports the cases passed and what the model was asked."""

CONTEXT_CHARS_CAP = 1500
PROMPT_CHARS_CAP = 8500


def score_case(fn, case):
    got = fn(case)
    wrong = []
    if sorted(got["cited"]) != sorted(case["expect_cited"]):
        wrong.append("cited ids")
    if got["context_chars"] > CONTEXT_CHARS_CAP:
        wrong.append("context over its cap")
    if got["max_prompt_chars"] > PROMPT_CHARS_CAP:
        wrong.append("a prompt over its cap")
    if got["rerun_calls"] != case.get("expect_rerun_calls", 0):
        wrong.append("the second read called the model more or less than a failed window's retry")
    for group, status in case.get("expect_status", {}).items():
        if got["statuses"].get(group) != status:
            wrong.append(f"status of {group}")
    return dict(ok=not wrong, calls=got["calls"], prompt_chars=got["max_prompt_chars"], note="ok" if not wrong else "differs: " + ", ".join(wrong))


def aggregate(rows):
    passed = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    largest = max((r.get("prompt_chars", 0) for r in rows), default=0)
    return f"cases passed {passed}/{len(rows)}; model calls {calls} (stubbed, no spend); largest prompt {largest} characters"
