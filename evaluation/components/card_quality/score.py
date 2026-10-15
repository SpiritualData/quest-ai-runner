"""Score one card-quality case on the context view. For a labelled collection id: ``ok`` when it is in the view AND no
learned collection outside ``{expect_id} + allowed_extra`` resolved (``allowed_extra`` is a sibling collection that the
SAME card legitimately carries). For ``expect_id: null`` (an unrelated query): ``ok`` when no learned collection id is in
the view. ``irrelevant`` counts the collections that resolved without being the labelled or an allowed one: spraying
every card into every query is a failure, not a pass."""
import json
from pathlib import Path

WORLD = json.loads((Path(__file__).parent / "world.json").read_text())


def score_case(fn, case):
    out = fn(case["query"], WORLD)
    view = out.get("view", "")
    resolved = [c for c in WORLD["collections"] if c in view]
    want = case.get("expect_id")
    if want is None:
        ok, note = not resolved, ("ok" if not resolved else f"leaked {resolved} into an unrelated query")
        irrelevant = len(resolved)
    else:
        extra = [c for c in resolved if c != want and c not in case.get("allowed_extra", [])]
        irrelevant = len(extra)
        ok = want in view and not extra
        note = "ok" if ok else (f"{want} not resolved; selected {out.get('titles')}" if want not in view
                                else f"{want} resolved but also pulled in unrelated {extra}")
    return dict(ok=ok, irrelevant=irrelevant, leak=want is None and bool(resolved),
                got=",".join(resolved) or "(none)", note=note)


def aggregate(rows):
    sel = [r for r in rows if r["id"] != "CRD-005"]
    return (f"selected+resolved {sum(r['ok'] for r in sel)}/{len(sel)}, "
            f"no-leak {sum(1 for r in rows if r['id'] == 'CRD-005' and r['ok'])}/1, "
            f"unrelated collections pulled in {sum(r['irrelevant'] for r in rows)}")
