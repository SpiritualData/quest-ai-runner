"""Score one judge call: the verdict must equal the label of the reference reply it graded. ``aggregate``
reports agreement per side (good replies passed, bad replies failed) and the two error kinds."""
import csv

import common

BEHAVIOR_CASES = {c["id"]: c for c in common.load_cases("esoi_behavior")}


def join(case):
    """The judge case joined to its behaviour case and the reference reply for its side."""
    behavior = BEHAVIOR_CASES[case["case_id"]]
    reply = behavior["good_reference" if case["side"] == "good" else "bad_reference"]
    return dict(case, behavior_case=behavior, reply=reply)


def score_case(fn, case):
    out = fn(join(case))
    verdict = str(out.get("verdict", "")).strip().lower()
    return dict(ok=verdict == case["expected"], got=case["side"],
                note=f"{verdict} (label {case['expected']}): {out.get('reason', '')}"[:200])


def aggregate(rows):
    sides = {}
    for r in rows:
        p, t = sides.get(r.get("got"), (0, 0))
        sides[r.get("got")] = (p + bool(r.get("ok")), t + 1)
    good, bad = sides.get("good", (0, 0)), sides.get("bad", (0, 0))
    return (f"agreement {good[0] + bad[0]}/{good[1] + bad[1]}; good replies passed {good[0]}/{good[1]} "
            f"(false fails {good[1] - good[0]}), bad replies failed {bad[0]}/{bad[1]} (false passes {bad[1] - bad[0]})")


def write_cases(path, behavior_cases):
    """Regenerate ``cases.csv`` from the behaviour cases (two rows each). Used when esoi_behavior grows."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "case_id", "side", "expected", "heldout"])
        for c in behavior_cases:
            for side, expected in (("good", "pass"), ("bad", "fail")):
                w.writerow([f"{c['id']}-{side}", c["id"], side, expected, "yes" if c.get("heldout") else ""])
