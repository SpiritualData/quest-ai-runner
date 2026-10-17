"""Score one chat-quality case from the record the live chat arm returns: pass means no hard pre-check failure and a
judge score at or above the pass threshold (``judge.case_passed``). An infrastructure error is a failure with its message."""


def score_case(fn, case):
    record = fn(case)
    if record.get("error"):
        return dict(ok=False, got="error", note=str(record["error"])[:200])
    hard = (record.get("precheck") or {}).get("hard_failures") or []
    score = record.get("score")
    note = "; ".join(hard) if hard else ((record.get("judged") or {}).get("summary") or "")[:120]
    return dict(ok=bool(record.get("passed")), got="score " + ("n/a" if score is None else f"{score:.2f}"), note=note)
