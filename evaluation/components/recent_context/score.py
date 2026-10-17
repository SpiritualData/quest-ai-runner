"""Score one recent-context case: ``ok`` when every labelled relevant card is returned and no forbidden card is.
``leak`` counts forbidden cards that came back (stale or cross-quest memory)."""


def score_case(fn, case):
    ids = fn(case).get("ids", [])
    found = [r for r in case["relevant"] if r in ids]
    leaked = [f for f in case["forbidden"] if f in ids]
    ok = len(found) == len(case["relevant"]) and not leaked
    return dict(ok=ok, leak=bool(leaked), got=",".join(ids) or "(none)",
                note="ok" if ok else (f"leaked {leaked}" if leaked else f"missing {sorted(set(case['relevant']) - set(found))}"))


def aggregate(rows):
    return f"warm-start right {sum(r['ok'] for r in rows)}/{len(rows)}, stale or cross-quest leaks {sum(bool(r.get('leak')) for r in rows)}"
