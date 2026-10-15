"""Score one locate case: ``ok`` when the labelled target path is in the answer."""


def score_case(fn, case):
    out = fn(case) or {}
    answer = out.get("answer") or ""
    ok = case["target"] in answer
    return dict(ok=ok, turns=out.get("turns"), got=answer[:90] or "(empty)",
                note="ok" if ok else f"target {case['target']} not in answer")


def aggregate(rows):
    turns = [r["turns"] for r in rows if r.get("turns") is not None]
    return (f"target found {sum(r['ok'] for r in rows)}/{len(rows)}"
            + (f", mean {sum(turns) / len(turns):.1f} tool rounds" if turns else ", no agent rounds (hint only)"))
