"""Score one chat-quality case from the record the live chat arm returns: pass means no hard pre-check failure and a
judge score at or above the pass threshold (``judge.case_passed``). An infrastructure error is a failure with its message.

The component's cases are every dataset case (``datasets/*.json``) plus the two smoke cases in ``cases.json``;
``load_cases`` is the one place that says so, so the offline arm covers all of them and the live arm draws from them."""


def load_cases():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner
    return runner.load_cases("all") + runner.load_example_cases()


def score_case(fn, case):
    record = fn(case)
    if record.get("error"):
        return dict(ok=False, got="error", note=str(record["error"])[:200])
    hard = (record.get("precheck") or {}).get("hard_failures") or []
    score = record.get("score")
    judged = record.get("judged") or {}
    summary = judged.get("summary") or (judged.get("verdict") or {}).get("summary") or ""
    note = "; ".join(hard) if hard else summary[:160]
    return dict(ok=bool(record.get("passed")), got="score " + ("n/a" if score is None else f"{score:.2f}"), note=note)
