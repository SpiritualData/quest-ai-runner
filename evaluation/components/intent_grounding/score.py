"""Score one intent-grounding case with the chat_quality verdict (hard checks by code, then the judge's rubric, pivots, routing
and side effects) and report the pass rate apart for the cases that depend on the intent and the controls that must not move."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def score_case(fn, case):
    record = fn(case)
    if record.get("error"):
        return dict(ok=False, got="error", note=str(record["error"])[:200], kind=case["kind"])
    hard = (record.get("precheck") or {}).get("hard_failures") or []
    score = record.get("score")
    judged = record.get("judged") or {}
    summary = (judged.get("verdict") or {}).get("summary") or judged.get("summary") or ""
    note = "; ".join(hard) if hard else summary[:160]
    failed = [r.get("item", "")[:90] for r in ((judged.get("verdict") or {}).get("rubric") or []) if not r.get("pass")]
    return dict(ok=bool(record.get("passed")), got="score " + ("n/a" if score is None else f"{score:.2f}"), note=note,
                kind=case["kind"], score=score, hard=bool(hard), reply=str(record.get("reply") or "")[:700], failed_items=failed)


def aggregate(rows):
    n = len(rows) or 1
    by_kind = {}
    for row in rows:
        k = by_kind.setdefault(row.get("kind", "?"), [0, 0, 0.0])
        k[0] += 1
        k[1] += 1 if row.get("ok") else 0
        k[2] += row.get("score") or 0.0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]} (mean score {v[2] / v[0]:.2f})" for kind, v in sorted(by_kind.items()))
    ok = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    hard = sum(1 for r in rows if r.get("hard"))
    return f"intent-grounded pass rate {ok}/{n} ({parts}); hard check failures {hard}; {calls / n:.1f} model calls per case (reply plus judge)"
