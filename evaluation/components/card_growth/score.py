"""Score one card-growth case: build the inflated store, let the solution settle it, then count what the index
still holds. A card is what ``FilesystemCardRepository`` would enumerate: a ``.json`` file directly in the store,
not a sidecar, not a dot file, not under ``_quarantine``. A source-file group is the set of paths a card pins.

A case passes when every group the store held is still held (nothing a lane knew is lost), each group is held
by exactly one card (duplicate share 0, cards per group 1.0), and the taught and managed cards are still there.
``aggregate`` reports cards per group, the duplicate share and the cards before and after."""
import json
import shutil
import tempfile
from pathlib import Path

from card_growth import fixtures

SIDECARS = {"bootstrap_meta.json", "folder_review.json", "discovered_files.json", "bootstrap_report_state.json"}


def indexed(cards_dir):
    out = {}
    for entry in sorted(Path(cards_dir).iterdir()):
        if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith(".") \
                and entry.name not in SIDECARS:
            c = json.loads(entry.read_text(encoding="utf-8"))
            out[str(c.get("id") or entry.stem)] = c
    return out


def group(c):
    return frozenset(f["path"] if isinstance(f, dict) else f for f in c.get("files") or [])


def score_case(fn, case):
    work = Path(tempfile.mkdtemp(prefix="qar_card_growth_"))
    try:
        cards_dir = work / ".quest-context"
        before = fixtures.write_store(cards_dir, case)
        fn(cards_dir)
        after = indexed(cards_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    groups_before = {group(c) for c in before.values()}
    groups_after = {group(c) for c in after.values()}
    lost = len(groups_before - groups_after)
    per_group = len(after) / max(len(groups_after), 1)
    dup_share = (len(after) - len(groups_after)) / max(len(after), 1)
    missing = [p for p in fixtures.PROTECTED if p not in after]
    problems = []
    if lost:
        problems.append(f"{lost} source-file groups lost")
    if dup_share:
        problems.append(f"{len(after) - len(groups_after)} duplicate cards left")
    if missing:
        problems.append(f"protected cards removed: {missing}")
    return dict(ok=not problems, before=len(before), after=len(after), groups=len(groups_before),
                per_group=round(per_group, 3), dup_share=round(dup_share, 3), lost=lost,
                got=f"{len(before)} -> {len(after)} cards over {len(groups_before)} groups",
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    before = sum(r.get("before", 0) for r in rows)
    after = sum(r.get("after", 0) for r in rows)
    groups = sum(r.get("groups", 0) for r in rows)
    worst = max((r.get("dup_share", 0) for r in rows), default=0)
    return (f"cards per group {after / max(groups, 1):.2f} (before {before / max(groups, 1):.2f}); "
            f"duplicate share worst case {worst:.0%}; cards {before} -> {after}; "
            f"groups lost {sum(r.get('lost', 0) for r in rows)}")
