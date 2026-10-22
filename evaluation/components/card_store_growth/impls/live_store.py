"""CURRENT: the live ``FileContextStore`` through its public surface, in nested temporary corpora with a fake model
(``world.py``). Zero model calls. The same file runs against an older checkout of the library, which is how a
"before" number is taken."""
import json
import logging
import shutil
import tempfile
from pathlib import Path

from card_store_growth import world


def new_store(root, cards=None, **kwargs):
    from quest_ai_runner.adapters.file_context_store import FileContextStore
    wanted = dict(kwargs)
    wanted.setdefault("max_cards", 0)
    wanted.setdefault("mount_mode", "copy")
    return FileContextStore(str(cards or root / ".quest-context"), repo_root=str(root), auto_bootstrap=False,
                            **{k: v for k, v in wanted.items() if world.store_accepts(k)})


def index(root, refresh=False, **kwargs):
    store = new_store(root, **kwargs)
    call = store.refresh_stale if refresh else store.bootstrap
    return call(root=str(root), provider=world.DiskProvider(root.resolve()))


def notes(root, folders):
    for name in folders:
        (root / name).mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (root / name / f"n{i}.md").write_text(f"# {name} {i}\nabout {name}\n")
    return root


def card_ids(root):
    return sorted(world.cards_in(root / ".quest-context"))


class Captured(logging.Handler):
    """The records the card limit logs, so the notice can be read as a person would."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        if getattr(record, "in_transcript", True):
            self.lines.append(record.getMessage())


def with_notice(work):
    handler = Captured()
    logger = logging.getLogger("quest-ai-runner.card-limit")
    logger.addHandler(handler)
    try:
        work()
    finally:
        logger.removeHandler(handler)
    text = " ".join(handler.lines)
    return all(part in text for part in ("topic", "imported", "learned", "quest", "QAR_MAX_CARDS", "ask the AI"))


def legacy(args, scratch):
    top = notes(scratch / "top", ["docs", "x"])
    mid = notes(top / "mid", ["auth", "search"])
    index(mid)
    index(top)
    clean = len(card_ids(top))
    planted = world.plant_legacy_copies(top, bool(args.get("with_provenance")))
    before = len(card_ids(top))
    start = new_store(top, self_heal=world.runner_self_heal())   # a runner's first start: open, then refresh
    start.prewarm()
    start.refresh_stale(root=str(top), provider=world.DiskProvider(top.resolve()))
    quarantine = top / ".quest-context" / "_quarantine"
    kept = len(list(quarantine.glob("*/*.json"))) if quarantine.is_dir() else 0
    after = len(card_ids(top))
    return {"clean": clean, "before": before, "after": after, "excess": after - clean,
            "kept_on_disk": int(kept == before - after)}


def limit(args, scratch):
    n, cap = int(args["folders"]), int(args["limit"])
    root = notes(scratch / "corpus", [f"f{i}" for i in range(n)])

    def run():
        for p in range(int(args["passes"])):
            store = new_store(root, max_cards=cap)
            call = store.refresh_stale if p else store.bootstrap
            call(root=str(root), provider=world.DiskProvider(root.resolve()))
    noticed = with_notice(run)
    cards = len(card_ids(root))
    return {"cards": cards, "limit": cap, "at_or_below_limit": int(cards <= cap),
            "notice_with_breakdown": int(noticed)}


def limit_learned(args, scratch):
    cap = int(args["limit"])
    root = notes(scratch / "corpus", [f"f{i}" for i in range(cap)])
    index(root, max_cards=cap)
    full = len(card_ids(root))
    store = new_store(root, max_cards=cap)
    for text in ("fix the chat bug", "add billing export", "explain search"):
        store.record(text, {"kind": "met", "files": []})
    quarantine = root / ".quest-context" / "_quarantine"
    moved = len(list(quarantine.glob("*/*.json"))) if quarantine.is_dir() else 0
    total = len(card_ids(root))
    return {"total": total, "total_at_limit": int(total <= cap), "nothing_deleted": int(total + moved == full + 3)}


def limit_open(args, scratch):
    root = scratch / "corpus"
    root.mkdir()
    cards = root / ".quest-context"
    cards.mkdir()
    for i in range(int(args["cards"])):
        (cards / f"topic-{i}.json").write_text(json.dumps({
            "id": f"topic-{i}", "name": "t", "summary": "t", "keywords": ["t"], "files": [
                {"path": f"f{i}/a.md"}, {"path": f"f{i}/b.md"}], "provenance": {"created_by_task": "bootstrap"}}))
    store = new_store(root, max_cards=int(args["limit"]))
    noticed = with_notice(store.prewarm)
    return {"notice_with_breakdown": int(noticed), "nothing_removed": int(len(card_ids(root)) == int(args["cards"]))}


NOTE = {"type": "note", "locator": {"text": "taught by a run"}, "why": "w"}


def write_card_file(cards, card_id, **fields):
    """A card as an older version or a person left it, written straight to the folder."""
    # A copy carries the name of the card it copies: the default name is the id without its import prefixes.
    card = {"id": card_id, "name": card_id.split("--")[-1].replace("card:", "", 1), "summary": f"about {card_id}",
            "keywords": [card_id], "files": [],
            "usage_count": 0, "provenance": {"created_by_task": "bootstrap"}}
    card.update(fields)
    cards.mkdir(parents=True, exist_ok=True)
    (cards / f"{card_id}.json").write_text(json.dumps(card), encoding="utf-8")


def holds_something(card):
    return world.holds_something(card)


def quarantined(root, reason=None):
    base = root / ".quest-context" / "_quarantine"
    pattern = f"{reason}/*.json" if reason else "*/*.json"
    out = []
    for path in sorted(base.glob(pattern)) if base.is_dir() else []:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def limit_protected(args, scratch):
    """A full store whose topic cards were all taught something: a card learned from a run moves none of them."""
    cap = int(args["limit"])
    root = notes(scratch / "corpus", [f"f{i}" for i in range(cap)])
    index(root, max_cards=cap)
    store = new_store(root, max_cards=cap)
    for i in range(cap):
        store.add_content(f"f{i}-topic", dict(NOTE, locator={"text": f"lesson {i}"}))
    for text in ("fix the chat bug", "add billing export", "explain search"):
        store.record(text, {"kind": "met", "files": []})
    moved = quarantined(root, "limit")
    live = world.cards_in(root / ".quest-context")
    return {"moved_that_hold_something": sum(1 for c in moved if holds_something(c)),
            "notes_kept": int(all(live.get(f"f{i}-topic", {}).get("content") for i in range(cap))),
            "learned_admitted": int(sum(1 for c in live.values() if not world.is_derived(c)) == 3)}


def tidy_protected(args, scratch):  # noqa: ARG001
    """Cards pinning one topic's files: only the plain copy may move, never one a person wrote or a run added to."""
    root = scratch / "corpus"
    cards = root / ".quest-context"
    files = [{"path": "docs/a.md"}, {"path": "docs/b.md"}]
    write_card_file(cards, "docs-topic", files=files)
    write_card_file(cards, "sd-aaaaaa--product-bbbbbb--docs-topic", files=files)                        # a plain copy
    write_card_file(cards, "release--checklist", files=files, provenance={})                            # by hand
    write_card_file(cards, "root-cccccc--sd-aaaaaa--root-cccccc--docs-topic", files=files,
                    content=[dict(NOTE, id="n1", ts=1.0)], name="docs-topic", summary="about docs-topic")  # a copy with a note
    write_card_file(cards, "sd-aaaaaa--docs-topic-2", files=files, scope_tags=["quest:q1"])             # tagged
    write_card_file(cards, "taught-docs", files=files, content=[dict(NOTE, id="n2", ts=1.0)])           # taught
    store = new_store(root, self_heal=True)
    store.tidy()
    live = world.cards_in(cards)
    moved = quarantined(root)
    note_kept = "n1" in {i.get("id") for c in live.values() for i in c.get("content") or []}
    return {"protected_moved": sum(1 for c in moved if holds_something(c) and not c.get("content")),
            "by_hand_kept": int("release--checklist" in live), "tagged_kept": int("sd-aaaaaa--docs-topic-2" in live),
            "taught_kept": int("taught-docs" in live), "copy_note_kept": int(note_kept),
            "plain_copy_moved": int("sd-aaaaaa--product-bbbbbb--docs-topic" not in live)}


def stale_writer(args, scratch):  # noqa: ARG001
    """An updater shown a card another process has since moved out of a full store: its note is kept, on the card
    as it was, and nobody else is moved for it."""
    root = notes(scratch / "corpus", ["a", "b", "c"])
    index(root, max_cards=3)
    stale = new_store(root, max_cards=3)
    stale._load_all()
    new_store(root, max_cards=3).update_card("lesson-1", add=[{"id": "l1", "type": "note", "text": "lesson"}])
    gone = [c["id"] for c in quarantined(root, "limit")]
    if gone:
        stale.update_card(gone[0], add=[{"id": "n1", "type": "note", "text": "late note"}])
    live = world.cards_in(root / ".quest-context")
    held = live.get(gone[0], {}) if gone else {}
    return {"moved_by_the_first": len(gone),
            "note_kept": int(any("n1" in {i.get("id") for i in c.get("content") or []} for c in live.values())),
            "card_is_not_a_stub": int(bool(held.get("summary"))),
            "nothing_else_moved": int(len(quarantined(root, "limit")) <= 1)}


def mounted_edit(args, scratch):  # noqa: ARG001
    """An edit meant for a card read in place from a neighbour store must not land on a card of this store."""
    top = notes(scratch / "top", ["api"])
    prod = notes(top / "prod", ["api"])
    index(prod)
    mounting = new_store(top, mount_mode="mount")
    mounting.bootstrap(root=str(top), provider=world.DiskProvider(top.resolve()))
    sid = json.loads((prod / ".quest-context" / "bootstrap_meta.json").read_text())["store_id"]
    mounting.update_card(f"card:{sid}:api-topic", add=[{"id": "z", "type": "note", "text": "for the neighbour"}])
    own = world.cards_in(top / ".quest-context").get("api-topic", {})
    theirs = world.cards_in(prod / ".quest-context").get("api-topic", {})
    return {"own_card_untouched": int(not own.get("content")), "neighbour_card_untouched": int(not theirs.get("content"))}


def old_copy_with_notes(args, scratch):  # noqa: ARG001
    """A copy an older version made, taught something, that its replacement now stands for."""
    top = notes(scratch / "top", ["docs"])
    prod = notes(top / "prod", ["api"])
    index(prod)
    index(top)
    cards = top / ".quest-context"
    live = world.cards_in(cards)
    replacement = next(cid for cid, c in live.items() if world.is_copy(c))
    old = json.loads(json.dumps(live[replacement]))
    old.update({"id": "legacy--api-topic", "usage_count": 3, "content": [dict(NOTE, id="old-note", ts=1.0)],
                "scope_tags": ["quest:q1"], "provenance": {"created_by_task": "bootstrap", "imported_from": "prod"}})
    (cards / "legacy--api-topic.json").write_text(json.dumps(old), encoding="utf-8")
    index(top, refresh=True)
    after = world.cards_in(cards)
    kept = after.get(replacement, {})
    return {"retired": int("legacy--api-topic" not in after),
            "notes_joined": int("old-note" in {i.get("id") for i in kept.get("content") or []}),
            "tags_joined": int("quest:q1" in (kept.get("scope_tags") or [])),
            "usage_joined": int(kept.get("usage_count", 0) == 3)}


def moved_corpus(args, scratch):  # noqa: ARG001
    """The corpus folder is renamed with its store inside it: the store keeps indexing."""
    old = notes(scratch / "corpus", ["a", "b"])
    index(old)
    new = scratch / "renamed"
    old.rename(new)
    notes(new, ["c"])
    return {"indexed_after_rename": int(index(new, refresh=True) >= 1), "cards": len(card_ids(new))}


def fresh_bootstrap_cost(args, scratch):
    """A corpus with more folders than the limit allows: the model is asked about the room, not about every folder."""
    folders, cap = int(args["folders"]), int(args["limit"])
    root = notes(scratch / "corpus", [f"f{i:02d}" for i in range(folders)])
    inner = world.DiskProvider(root.resolve())

    class Counting:
        calls = 0

        def list_models(self):
            return []

        def answer(self, messages, model=None, **kwargs):
            Counting.calls += 1
            return inner.answer(messages, model=model, **kwargs)
    store = new_store(root, max_cards=cap)
    store.bootstrap(root=str(root), provider=Counting())
    first = Counting.calls
    new_store(root, max_cards=cap).refresh_stale(root=str(root), provider=Counting())
    refused = len(getattr(store.limit_guard, "refused_ids", ()))
    return {"calls": first, "calls_at_most_one_per_folder_of_room": int(first <= 2 * cap + 1), "refused": refused,
            "second_pass_calls": Counting.calls - first, "second_pass_free": int(Counting.calls == first)}


def forked(count, target, *extra):
    """Run ``target(index, barrier, results, *extra)`` in ``count`` processes started together; the results, sorted."""
    import multiprocessing
    context = multiprocessing.get_context("fork")
    barrier, results = context.Barrier(count), context.Queue()
    procs = [context.Process(target=target, args=(n, barrier, results) + extra) for n in range(count)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=300)
    return sorted(results.get() for _ in procs)


def tidy_in_process(number, barrier, results, root):  # noqa: ARG001
    logging.disable(logging.CRITICAL)
    store = new_store(Path(root), self_heal=True)
    barrier.wait()
    results.put(store.tidy())


def concurrent_tidy(args, scratch):
    """Four processes tidy one store at once: every file is kept, every move is counted once, every note folded once."""
    count = int(args["cards"])
    root = scratch / "corpus"
    cards = root / ".quest-context"
    for i in range(count):
        files = [{"path": f"f{i}/a.md"}, {"path": f"f{i}/b.md"}]
        write_card_file(cards, f"f{i}-topic", files=files, name=f"f{i}",
                        provenance={"created_by_task": "bootstrap", "created_at": "2026-01-01T00:00:00+00:00"})
        write_card_file(cards, f"f{i}-dup", files=files, name=f"f{i}",
                        provenance={"created_by_task": "bootstrap", "created_at": "2026-02-01T00:00:00+00:00"})
        write_card_file(cards, f"card:f{i}-topic", name=f"f{i}", summary=f"about f{i}-topic",
                        content=[dict(NOTE, id=f"c{i}", ts=1.0)])
    before = len(list(cards.glob("*.json")))
    counted = forked(4, tidy_in_process, str(root))
    live = len(list(cards.glob("*.json")))
    moved = len(list(cards.glob("_quarantine/*/*.json")))
    notes_once = all([i.get("id") for i in json.loads((cards / f"f{i}-topic.json").read_text()).get("content") or []]
                     == [f"c{i}"] for i in range(count))
    claimed = 0
    for manifest in cards.glob("_quarantine/*tidy-manifest*"):
        text = manifest.read_text(encoding="utf-8")
        if manifest.suffix == ".jsonl":
            claimed += sum(1 for line in text.splitlines() if "moved_to" in line)
        else:
            claimed += int(json.loads(text).get("moved", 0))
    return {"files_conserved": int(live + moved == before), "moves_counted_once": int(sum(counted) == moved),
            "manifests_name_every_move": int(claimed == moved), "notes_folded_once": int(notes_once)}


def write_in_process(number, barrier, results, root, cap):
    logging.disable(logging.CRITICAL)
    store = new_store(Path(root), max_cards=cap)
    store._load_all()
    store.limit_guard.snapshot()
    barrier.wait()
    written = 0
    for i in range(10):
        written += bool(store.write_card(f"p{number}-learned-{i}", {"name": "l", "content": [dict(NOTE, id="x", ts=1.0)]}))
    results.put(written)


def concurrent_limit(args, scratch):
    """Four processes learn cards at a store 5 below its limit: the limit holds and every move is counted once."""
    cap = int(args["limit"])
    root = scratch / "corpus"
    cards = root / ".quest-context"
    for i in range(cap - 5):
        write_card_file(cards, f"t{i:03d}", files=[{"path": f"f{i}/a.md"}, {"path": f"f{i}/b.md"}])
    written = sum(forked(4, write_in_process, str(root), cap))
    live = len(list(cards.glob("*.json")))
    moved = list(cards.glob("_quarantine/limit/*.json"))
    manifest = cards / "_quarantine" / "limit" / "manifest.jsonl"
    lines = manifest.read_text().splitlines() if manifest.is_file() else []
    return {"limit_held": int(live <= cap), "all_written": int(written == 40), "moves_counted_once": int(len(moved) == len(lines)),
            "nothing_deleted": int(live + len(moved) == cap - 5 + written)}


def build(meter):  # noqa: ARG001
    def grow(kind, args):
        if kind == "fuzz":
            outcome = world.run(int(args["seed"]), int(args.get("steps", 20)))
            return {"violations": len(outcome["violations"]),
                    "first": (outcome["violations"] or [""])[0][:160]}
        builders = {"legacy": legacy, "limit": limit, "limit_learned": limit_learned, "limit_open": limit_open,
                    "limit_protected": limit_protected, "tidy_protected": tidy_protected,
                    "stale_writer": stale_writer, "mounted_edit": mounted_edit,
                    "old_copy_with_notes": old_copy_with_notes, "moved_corpus": moved_corpus,
                    "fresh_bootstrap_cost": fresh_bootstrap_cost, "concurrent_tidy": concurrent_tidy,
                    "concurrent_limit": concurrent_limit}
        scratch = Path(tempfile.mkdtemp(prefix="qar_card_store_growth_"))
        quiet = logging.NullHandler()      # the library's own warnings are read through ``Captured``, not printed
        logging.getLogger("quest-ai-runner").addHandler(quiet)
        try:
            with world.offline_git():
                return builders[kind](args, scratch)
        finally:
            logging.getLogger("quest-ai-runner").removeHandler(quiet)
            shutil.rmtree(scratch, ignore_errors=True)
    return grow
