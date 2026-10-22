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
    return FileContextStore(str(cards or root / ".quest-context"), repo_root=str(root), auto_bootstrap=False,
                            mount_mode="copy", **{k: v for k, v in wanted.items() if world.store_accepts(k)})


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
    return all(part in text for part in ("topic cards", "imported from other stores", "learned from runs",
                                         "QAR_MAX_CARDS", "ask the AI"))


def legacy(args, scratch):
    top = notes(scratch / "top", ["docs", "x"])
    mid = notes(top / "mid", ["auth", "search"])
    index(mid)
    index(top)
    clean = len(card_ids(top))
    planted = world.plant_legacy_copies(top, bool(args.get("with_provenance")))
    before = len(card_ids(top))
    start = new_store(top, self_heal=True)           # a runner's first start: open, then refresh
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


def build(meter):  # noqa: ARG001
    def grow(kind, args):
        if kind == "fuzz":
            outcome = world.run(int(args["seed"]), int(args.get("steps", 20)))
            return {"violations": len(outcome["violations"]),
                    "first": (outcome["violations"] or [""])[0][:160]}
        builders = {"legacy": legacy, "limit": limit, "limit_learned": limit_learned, "limit_open": limit_open}
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
