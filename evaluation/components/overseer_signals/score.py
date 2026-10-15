"""Score one overseer scenario. ``digest_ok``: the digest built from ``digest_args`` shows every string in
``must_show`` and stays within the 1600-char budget. ``sig_ok``: the solution's signal is one of ``want``
(only when the solution returned one). ``ok`` needs both. ``aggregate`` reports the two apart, because an
offline run measures only the first."""
from quest_ai_runner.core.overseer import build_digest

BUDGET = 1600


def score_case(fn, case):
    digest = build_digest(**case["digest_args"])
    missing = [s for s in case["must_show"] if s not in digest]
    digest_ok = not missing and len(digest) <= BUDGET
    out = fn(digest) or {}
    sig = out.get("signal")
    sig_ok = None if sig is None else sig in case["want"]
    notes = []
    if missing:
        notes.append(f"digest lacks {missing}")
    if len(digest) > BUDGET:
        notes.append(f"digest is {len(digest)} chars")
    if sig_ok is False:
        notes.append(f"want {'|'.join(case['want'])}, got {sig}: {out.get('reason', '')}")
    return dict(ok=digest_ok and sig_ok is not False, digest_ok=digest_ok, sig_ok=sig_ok,
                got=sig or "(digest only)", note="; ".join(notes) or "ok")


def aggregate(rows):
    d = sum(bool(r.get("digest_ok")) for r in rows)
    answered = [r for r in rows if r.get("sig_ok") is not None]
    sig = f"signal {sum(bool(r['sig_ok']) for r in answered)}/{len(answered)}" if answered else "signal not measured (no model)"
    return f"digest evidence {d}/{len(rows)}, {sig}"
