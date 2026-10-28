"""Score one image question by the labelled answer in the reply, and weigh it.

Per case: ``ok`` (the reply matches the labelled answer), ``described`` (a vision model was asked), ``failed_description`` (the
describer ran and the library fell back to its note). The value of the description is the gain between the two arms on the same
cases (read by ``justified_model_spend``); in a control the message alone answers the question, so a description changes no verdict.

``aggregate`` reads the whole run: pass rate by kind (value), images described and how many of those were on controls, where the
description is cost with nothing to gain (cost), and model calls per case (time proxy: the description is awaited before the answer)."""
import re


def score_case(fn, case):
    got = fn(case)
    reply = got["reply"]
    ok = any(re.search(p, reply, re.I) for p in case["expect_any"])
    failed = "could not be transcribed" in got["grounding"]
    return dict(ok=ok, got=reply[:80].replace("\n", " "), want=case["expect_any"][0], kind=case["kind"], described=got["described"],
                failed_description=failed, note="ok" if ok else f"reply lacks the labelled answer {case['expect_any'][0]!r}")


def aggregate(rows):
    n = len(rows) or 1
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]}" for kind, v in sorted(by_kind.items()))
    described = [r for r in rows if r.get("described")]
    wasted = sum(1 for r in described if r.get("kind") == "control")
    calls = sum(r.get("calls", 0) for r in rows)
    return (f"value {sum(1 for r in rows if r.get('ok'))}/{n} answered as labelled ({parts}); images described {len(described)}, of which on "
            f"controls where the message alone answers {wasted}, failed descriptions {sum(1 for r in rows if r.get('failed_description'))}; "
            f"cost {calls / n:.2f} model calls per case (the description is awaited before the answer)")
