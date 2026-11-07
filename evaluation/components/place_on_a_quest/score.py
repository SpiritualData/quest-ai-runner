"""Scores a quest placement arm with ONE number per case: how much of the labelled placement it got.

A case is worth 1.0 when the quests picked are exactly the labelled ones (the empty list for a message no quest fits), else the Jaccard overlap of
the two sets (0.0 for a pick when the label is none, and 0.0 for none when the label names a quest). The kinds of case (``named``, ``topic``,
``cue``, ``distractor``, ``none``, ``multi``, ``followup``) are sources of this one metric, mixed as listed in ``cases.json``; none of them has a
pass test of its own. Besides the accuracy a row reports the label's rank in the arm's candidate list, so a first stage that only generates
candidates is read on the same number (accuracy of what it would pick) and on whether the right quest was among the first three.
The balanced number combines the exact-match rate (the cases worth 1.0), cost and time by ``[balance.place_on_a_quest]`` in esoi.toml (weights fixed before any arm was scored)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases():
    """The public cases, then the real ones when a private labelled file is present (none otherwise)."""
    from place_on_a_quest import private_cases
    return json.loads((HERE / "cases.json").read_text()) + private_cases.load_cases()


def jaccard(got, want):
    got, want = set(got), set(want)
    return 1.0 if got == want else len(got & want) / len(got | want)


def score_case(fn, case):
    answer = fn(case)
    picked, ranked = list(answer.get("picked") or []), list(answer.get("ranked") or [])
    want = list(case["expect"])
    value = jaccard(picked, want)
    in_three = (not want) or all(q in ranked[:3] for q in want)
    return dict(ok=value == 1.0, value=round(value, 4), got=picked, want=want, source=case["source"], in_top_three=in_three,
                calls=int(answer.get("calls") or 0), note="ok" if value == 1.0 else f"picked {picked}, labelled {want}")


def aggregate(rows):
    sources = sorted({r["source"] for r in rows})
    by = [f"{s} {sum(r['value'] for r in rows if r['source'] == s):.1f}/{sum(r['source'] == s for r in rows)}" for s in sources]
    accuracy = sum(r["value"] for r in rows) / max(len(rows), 1)
    real = [r for r in rows if r["source"] == "real"]
    if real:
        from place_on_a_quest import private_cases
        share = private_cases.share()
        others = [r for r in rows if r["source"] != "real"]
        accuracy = (1 - share) * sum(r["value"] for r in others) / max(len(others), 1) + share * sum(r["value"] for r in real) / len(real)
    top3 = sum(bool(r["in_top_three"]) for r in rows) / max(len(rows), 1)
    return (f"accuracy {accuracy:.4f} | right quest within the first three {top3:.4f} | model calls {sum(r['calls'] for r in rows)} | "
            "case sources " + " | ".join(by))
