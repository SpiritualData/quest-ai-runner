"""Score one task. The ideal says every Quest task is under a measured choice (``expect_measured`` true): such a case passes only
when the function reports it measured AND gives the recorded accuracy it rests on (a claim with no number is not a measurement).
A trap case (``expect_measured`` false: a component that does not exist, an arm never recorded, a row under the bar) passes only
when the function reports it not measured, so a function that overclaims fails the traps. ``aggregate`` names the real tasks that
have no labelled cases or recorded accuracy, so the gap reads as a list and not a number."""


def score_case(fn, case):
    got = fn(case)
    measured = bool(got["measured"]) and got.get("rate") is not None
    ok = measured if case["expect_measured"] else not got["measured"]
    return dict(ok=ok, task=case["task"], trap=not case["expect_measured"], rate=got.get("rate"),
                got="measured" if got["measured"] else "not measured", note=got.get("why", ""))


def aggregate(rows):
    real = [r for r in rows if not r.get("trap")]
    ok = [r for r in real if r.get("ok")]
    lacking = [r["task"] for r in real if not r.get("ok")]
    traps = [r for r in rows if r.get("trap")]
    return (f"{len(ok)}/{len(real)} tasks under a measured choice, {sum(1 for r in traps if r.get('ok'))}/{len(traps)} overclaims refused"
            + (f"; no labelled cases or recorded accuracy for: {', '.join(lacking)}" if lacking else ""))
