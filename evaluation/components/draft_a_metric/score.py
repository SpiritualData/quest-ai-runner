"""Score one metric draft against the label its source states, and weigh value against cost and time.

Per case: ``ok`` (the draft is the labelled one: a value equal to the labelled value for every question, with at least one cited
piece of evidence that is in the card, or no proposal when the label says the evidence is too thin), ``invented`` (a value was
proposed where the label says the evidence is too thin: the error the prompt calls worse than a missing value), ``ungrounded`` (a
proposal whose cited evidence does not appear in the card), ``over_cap`` (more model calls than the module's cap).

``aggregate`` reads the whole run: share correct (value), model calls per case with the cap the module states (cost), and the fact that
a draft runs as a queued task, off the reply path (time)."""
import json
import re
from pathlib import Path

from quest_ai_runner.runner import metric_draft

HERE = Path(__file__).resolve().parent
WORD = re.compile(r"[a-z0-9]+")


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def card_words(card):
    return set(WORD.findall(json.dumps(card, ensure_ascii=False).lower()))


def grounded(evidence, card):
    """Every cited piece of evidence shares most of its words with the card: a draft cannot cite what the data does not hold."""
    have = card_words(card)
    for item in evidence:
        words = WORD.findall(str((item or {}).get("summary") or "").lower())
        if not words or sum(1 for w in words if w in have) / len(words) < 0.6:
            return False
    return bool(evidence)


def same_values(have, want):
    try:
        return set(have or {}) == set(want) and all(abs(float(have[k]) - float(want[k])) < 1e-9 for k in want)
    except (TypeError, ValueError):
        return False


def score_case(fn, case):
    got = fn(case)
    label = case["label"]
    proposed = got["status"] == "proposed"
    over_cap = got.get("model_calls", 0) > metric_draft.MAX_MODEL_CALLS
    if label["propose"]:
        ok = proposed and same_values(got["values"], label["values"]) and grounded(got["evidence"], case["card"])
        note = "ok" if ok else (f"{got['status']}: {got['text'][:80]}" if not proposed else f"values {got['values']}, labelled {label['values']}")
        return dict(ok=ok and not over_cap, got=str((got["values"] or {})) if proposed else got["status"], want=str(label["values"]),
                    invented=False, ungrounded=bool(proposed and not grounded(got["evidence"], case["card"])), over_cap=over_cap,
                    kind=case["kind"], note=note)
    ok = not proposed
    return dict(ok=ok and not over_cap, got=str(got["values"]) if proposed else "nothing", want="nothing", invented=proposed,
                ungrounded=bool(proposed and not grounded(got["evidence"], case["card"])), over_cap=over_cap, kind=case["kind"],
                note="ok" if ok else f"proposed {got['values']} where the evidence is thin")


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    invented = sum(1 for r in rows if r.get("invented"))
    ungrounded = sum(1 for r in rows if r.get("ungrounded"))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]}" for kind, v in sorted(by_kind.items()))
    return (f"value {ok}/{n} drafts as labelled ({parts}); invented values on thin evidence {invented}; ungrounded evidence {ungrounded}; "
            f"cost {calls / n:.2f} model calls per case (cap {metric_draft.MAX_MODEL_CALLS} per task); a draft is a queued task, off the "
            f"reply path; labels are source fixtures, not owner acceptances")
