"""Score one tag case. A ``check`` case expects exactly the problem lines named by ``expect.fails`` (each a fragment of
one line; an empty list means the tag file passes) and, when given, the notes named by ``expect.notes``. A ``lookup``
case expects exactly the solutions in ``expect.found``, in record order."""


def score_case(fn, case):
    got = fn(case["kind"], case["args"])
    expect = case["expect"]
    if case["kind"] == "lookup":
        ok = got["found"] == expect["found"]
        return dict(ok=ok, got=f"found {got['found']}", note="ok" if ok else f"expected {expect['found']}")
    lines, wanted = got["problems"], expect.get("fails", [])
    missing = [w for w in wanted if not any(w in line for line in lines)]
    extra = len(lines) - len(wanted)
    problems = [f"did not fail on: {m}" for m in missing]
    if extra > 0 or (not missing and extra < 0):
        problems.append(f"{len(lines)} problem line(s), expected {len(wanted)}: {lines}")
    for note in expect.get("notes", []):
        if not any(note in n for n in got.get("notes", [])):
            problems.append(f"no note: {note}")
    return dict(ok=not problems, got=f"{len(lines)} problem line(s)", note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    kinds = {}
    for r in rows:
        kinds.setdefault(r.get("area") or "", []).append(bool(r["ok"]))
    return ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(kinds.items()))
