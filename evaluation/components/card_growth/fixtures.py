"""A card store inflated the way copy-import inflated real ones, rebuilt offline with no randomness.

Two stores sit one inside the other: ``outer`` indexes ``corpus`` and ``inner`` indexes ``corpus/org``. In copy
mode each bootstrap pass copies the other store's CURRENT cards under ``<namespace of the source root>--<id>``,
cut to 200 characters (the rule of ``context_store_bootstrap.py`` before the guards), and the inner store copies
only the outer cards whose files lie under its own root. Copies of copies are kept, so a card the inner store
owns comes back into the outer store one namespace longer every second pass. Paths are kept in the outer
store's coordinates so two cards for the same files compare equal.

Every store also holds what a cleanup must leave alone: a card a person taught (used, written by a task), a card
another system manages, and, when the case asks for it, two bootstrap cards that pin the same file (surplus).
"""
import json

from quest_ai_runner.adapters.context_card_extraction import _path_slug

OUTER_ROOT, INNER_ROOT = "corpus", "corpus/org"
INNER_TOPICS = ("poller", "executor", "ledger", "planner", "router", "budget")
OUTER_TOPICS = ("readme", "roadmap", "glossary", "changelog")
PROTECTED = ("taught-weekly-review", "managed-quest-folder")


def card(card_id, files, **extra):
    out = dict(id=card_id, name=card_id, keywords=[card_id], summary=f"{card_id} topic",
               files=[{"path": p} for p in files], usage_count=0, provenance={"created_by_task": "bootstrap"})
    out.update(extra)
    return out


def own_cards(surplus):
    inner = {t: card(t, [f"org/{t}.py"]) for t in INNER_TOPICS}
    outer = {t: card(t, [f"{t}.md"]) for t in OUTER_TOPICS}
    outer["taught-weekly-review"] = card("taught-weekly-review", ["notes/weekly.md"], usage_count=3,
                                         provenance={"created_by_task": "task-1"})
    outer["managed-quest-folder"] = card("managed-quest-folder", ["quests/q1.md"], managed_by="quest-folder-sync")
    if surplus:
        outer["shared-util-a"] = card("shared-util-a", ["shared/util.py"])
        outer["shared-util-b"] = card("shared-util-b", ["shared/util.py"])
    return outer, inner


def copy_of(source_card, namespace, imported_from):
    cid = f"{namespace}--{source_card['id']}"[:200]
    out = dict(source_card, id=cid, name=source_card["name"])
    out["provenance"] = {"created_by_task": "bootstrap", "imported_from": imported_from}
    return cid, out


def inflate(passes, surplus):
    """The outer store's cards after ``passes`` copy-mode bootstrap passes over both stores."""
    outer_own, inner_own = own_cards(surplus)
    outer, inner = dict(outer_own), dict(inner_own)
    outer_ns, inner_ns = _path_slug(OUTER_ROOT), _path_slug(INNER_ROOT)
    for _ in range(passes):
        new_outer, new_inner = dict(outer_own), dict(inner_own)
        for c in inner.values():
            cid, copied = copy_of(c, inner_ns, "org")
            new_outer[cid] = copied
        for c in outer.values():
            paths = [f["path"] for f in c["files"]]
            if paths and all(p.startswith("org/") for p in paths):
                cid, copied = copy_of(c, outer_ns, "..")
                new_inner[cid] = copied
        outer, inner = new_outer, new_inner
    return outer


def write_store(cards_dir, case):
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards = inflate(case["passes"], case.get("surplus", False))
    for cid, c in cards.items():
        (cards_dir / f"{cid}.json").write_text(json.dumps(c), encoding="utf-8")
    (cards_dir / "bootstrap_meta.json").write_text(json.dumps({"root": OUTER_ROOT}), encoding="utf-8")
    return cards
