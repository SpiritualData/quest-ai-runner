"""Score one select_a_rep case: the chosen representative id against the labelled one.

Counted per case: ``bare_mention`` (the case was a bare mention of a representative's name and one was chosen anyway),
``hidden_chosen`` (a representative the person cannot see was chosen). A case passes when the chosen id equals the
label (None meaning nobody)."""


def score_case(fn, case):
    got = fn(case).get("rep_id")
    want = case.get("expect")
    ok = got == want
    return dict(ok=ok, got=str(got), want=str(want), area=case.get("area"),
                bare_mention=bool(case.get("bare_mention") and got is not None),
                hidden_chosen=bool(case.get("hidden_rep") and got == case.get("hidden_rep")),
                known_gap=bool(case.get("known_gap")),
                note="ok" if ok else f"chose {got!r}, labelled {want!r}")


def aggregate(rows):
    bare = sum(1 for r in rows if r.get("bare_mention"))
    hidden = sum(1 for r in rows if r.get("hidden_chosen"))
    gaps = sum(1 for r in rows if r.get("known_gap") and not r.get("ok"))
    return (f"selections by a bare mention {bare}; a representative the person cannot see chosen {hidden}; "
            f"known-gap cases failing {gaps}; questions asked 0; model calls 0")
