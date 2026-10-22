"""Card-store commands: ``quest-ai-runner cards report`` and ``quest-ai-runner cards tidy``.

Split out of ``cli.py`` by definition, one module per command family; that module re-exports every name here.
"""
from __future__ import annotations

import os
from pathlib import Path


def run_cards_command(args, log) -> int:
    """Run the `cards` subcommand; returns the process exit code."""
    from .adapters import card_limit
    from .adapters.card_limit_report import report_for, write_report

    corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
    cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")
    if not os.path.isdir(cards_dir):
        print(f"error: {cards_dir} is not a card store (a folder of card files). Pass --cards-dir.")
        return 2
    if getattr(args, "cards_op", "report") == "tidy":
        undo = getattr(args, "undo", None)
        if undo:
            return undo_cards(Path(cards_dir), Path(undo))
        return tidy_cards(Path(cards_dir), apply=bool(getattr(args, "apply", False)))
    text = report_for(cards_dir, card_limit.max_cards_from_env())
    print(text)
    if args.write:
        from .adapters.card_limit_report import REPORT_NAME
        if write_report(Path(cards_dir), text):
            print(f"Written to {Path(cards_dir) / REPORT_NAME}")
    return 0


def tidy_cards(cards_dir: Path, *, apply: bool) -> int:
    """List (or, with ``apply``, carry out) the moves the store's own tidy makes. Nothing is ever deleted."""
    from .adapters import card_store_hygiene as hygiene
    from .adapters.card_store_lock import TIDY_LOCK_FILE, StoreLock

    own_store_id = hygiene.store_id_of(cards_dir)
    plan = hygiene.plan_tidy(hygiene.card_files_in(cards_dir), own_store_id=own_store_id)
    print(f"cards looked at : {plan.scanned:,}")
    print(f"would move      : {len(plan.moves):,}" + (
        "  (" + ", ".join(f"{name} {n:,}" for name, n in sorted(plan.by_reason().items())) + ")"
        if plan.moves else ""))
    if plan.kept:
        print("left in place on purpose:")
        for why, count in sorted(plan.kept.items()):
            print(f"  {count:>8,}  {why}")
    if not apply:
        print("This was a dry run, nothing moved. Add --apply to move these cards; they go to "
              f"{cards_dir / hygiene.QUARANTINE_DIR_NAME}/<reason>/ and moving the files back undoes it.")
        return 0
    if not plan.moves:
        return 0
    process_lock = StoreLock.for_dir(str(cards_dir), TIDY_LOCK_FILE)
    if not process_lock.acquire(blocking=False):
        print("Another process is tidying this store right now. Try again in a minute.")
        return 1
    try:
        result = hygiene.apply_tidy(cards_dir, plan, own_store_id=own_store_id)
    finally:
        process_lock.release()
    print(f"moved           : {result.moved:,}" + (f"  ({result.failed:,} could not be moved)" if result.failed else ""))
    for why, count in sorted(result.left.items()):
        print(f"  {count:>8,}  not moved, it changed since the scan: {why}")
    if result.manifest:
        print(f"manifest        : {result.manifest}")
    return 0


def undo_cards(cards_dir: Path, manifest: Path) -> int:
    """Take back the moves a tidy manifest records. Moving the files back by hand puts the cards back but not
    what was joined onto the cards that stood for them (each joined note would then exist twice); this does both."""
    from .adapters import card_store_hygiene as hygiene
    from .adapters.card_store_lock import TIDY_LOCK_FILE, StoreLock

    if not manifest.is_file():
        print(f"error: {manifest} is not a file. The manifests are the *-tidy-manifest.jsonl files in "
              f"{cards_dir / hygiene.QUARANTINE_DIR_NAME}.")
        return 2
    process_lock = StoreLock.for_dir(str(cards_dir), TIDY_LOCK_FILE)
    if not process_lock.acquire(blocking=False):
        print("Another process is tidying this store right now. Try again in a minute.")
        return 1
    try:
        result = hygiene.undo_tidy(cards_dir, manifest)
    finally:
        process_lock.release()
    print(f"put back        : {result.restored:,}")
    print(f"taken off again : {result.unjoined:,} joins onto the cards that stood for them")
    for why, count in sorted(result.skipped.items()):
        print(f"  {count:>8,}  skipped: {why}")
    return 0
