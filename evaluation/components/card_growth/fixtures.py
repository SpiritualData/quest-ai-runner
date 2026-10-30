"""A card store inflated the way copy-import inflated real ones, rebuilt offline with no randomness.

Two stores sit one inside the other: ``outer`` indexes ``corpus`` and ``inner`` indexes ``corpus/org``. In copy
mode each bootstrap pass copies the other store's CURRENT cards under ``<namespace of the source root>--<id>``,
cut to 200 characters (the rule of ``context_store_bootstrap.py`` before the guards), and the inner store copies
only the outer cards whose files lie under its own root. Copies of copies are kept, so a card the inner store
owns comes back into the outer store one namespace longer every second pass. Paths are kept in the outer
store's coordinates so two cards for the same files compare equal.

Every store also holds what a cleanup must leave alone: a card a person taught (used, written by a task) and a card
another system manages. A case may add:

  surplus             two bootstrap cards for the same topic pinning the same file: one of them is a duplicate.
  person_card         a card a person wrote by hand on a file a topic card also pins, with no provenance and no
                      usage: unmarked, but nothing in the corpus can regenerate it, so it must stay.
  notes_on_copy       the most compounded copy of one inner topic carries a note a run added (``content``): the
                      copy may go, the note may not.
  two_topics_one_file two bootstrap cards for two DISTINCT topics of one file (different names, keywords and
                      summaries): not duplicates, both must stay.

A card's topic is its ``name``; a copy keeps the name of the card it copies, so a copy and its original are one
topic, and two cards are duplicates only when they pin the same files AND name the same topic.
"""
import json

from hubofhubs.recall.context_card_extraction import path_slug

OUTER_ROOT, INNER_ROOT = "corpus", "corpus/org"
INNER_TOPICS = ("poller", "executor", "ledger", "planner", "router", "budget")
OUTER_TOPICS = ("readme", "roadmap", "glossary", "changelog")
PROTECTED = ("taught-weekly-review", "managed-quest-folder")
PERSON_CARD = "meeting-notes-by-hand"
TWO_TOPICS = ("handbook-billing", "handbook-auth")
NOTE_TOPIC = "poller"
NOTE_TEXT = "a run learned: the poller retries a failed claim before it reports"


def protected(case):
    """The ids a cleanup must leave in the index for ``case``."""
    out = list(PROTECTED)
    if case.get("person_card"):
        out.append(PERSON_CARD)
    if case.get("two_topics_one_file"):
        out.extend(TWO_TOPICS)
    return out


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
    if surplus:       # one topic written twice: same name, keywords and summary
        for cid in ("shared-util-a", "shared-util-b"):
            outer[cid] = card(cid, ["shared/util.py"], name="shared util", keywords=["util", "shared"],
                              summary="Shared helper functions.")
    return outer, inner


def add_extras(outer, case):
    """The cards a case adds on top of the inflated store (see the module docstring)."""
    if case.get("person_card"):
        hand = card(PERSON_CARD, ["readme.md"], name="meeting notes", keywords=["meeting", "decisions"],
                    summary="What we decided about the readme in the weekly meeting.")
        hand.pop("provenance")
        outer[PERSON_CARD] = hand
    if case.get("two_topics_one_file"):
        outer[TWO_TOPICS[0]] = card(TWO_TOPICS[0], ["handbook.md"], name="handbook billing",
                                    keywords=["billing", "invoice"], summary="How the handbook says billing works.")
        outer[TWO_TOPICS[1]] = card(TWO_TOPICS[1], ["handbook.md"], name="handbook auth",
                                    keywords=["auth", "login"], summary="How the handbook says sign-in works.")
    if case.get("notes_on_copy"):
        copies = [cid for cid, c in outer.items() if c["name"] == NOTE_TOPIC and "--" in cid]
        target = max(copies, key=lambda cid: (cid.count("--"), cid))
        outer[target] = dict(outer[target], content=[{"id": "note-1", "type": "note", "locator": {"text": NOTE_TEXT},
                                                      "why": "learned in a run"}])
    return outer


def notes_kept(cards):
    """Whether the note a run added is still held by some card of the index."""
    return any(NOTE_TEXT in json.dumps(c.get("content") or []) for c in cards.values())


def copy_of(source_card, namespace, imported_from):
    cid = f"{namespace}--{source_card['id']}"[:200]
    out = dict(source_card, id=cid, name=source_card["name"])
    out["provenance"] = {"created_by_task": "bootstrap", "imported_from": imported_from}
    return cid, out


def inflate(passes, surplus):
    """The outer store's cards after ``passes`` copy-mode bootstrap passes over both stores."""
    outer_own, inner_own = own_cards(surplus)
    outer, inner = dict(outer_own), dict(inner_own)
    outer_ns, inner_ns = path_slug(OUTER_ROOT), path_slug(INNER_ROOT)
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
    cards = add_extras(inflate(case["passes"], case.get("surplus", False)), case)
    for cid, c in cards.items():
        (cards_dir / f"{cid}.json").write_text(json.dumps(c), encoding="utf-8")
    (cards_dir / "bootstrap_meta.json").write_text(json.dumps({"root": OUTER_ROOT}), encoding="utf-8")
    return cards
