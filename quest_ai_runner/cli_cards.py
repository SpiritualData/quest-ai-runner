"""Card-store commands: ``quest-ai-runner cards report`` and ``quest-ai-runner cards tidy``.

Split out of ``cli.py`` by definition, one module per command family; that module re-exports every name here.
"""
from __future__ import annotations

import os
from pathlib import Path


def lane_store(args):
    """``(cards_dir, corpus, repository or None)`` of the lane the command is about. The lane's config file (``--config``
    or ``QAR_CONFIG_FILE``) puts its environment in place first, so a command run by hand reaches the store the lane
    uses: ``None`` for the default local folder, else the Quest backend or Qdrant repository."""
    from .config import RunnerConfig
    from .config_assembler import build_card_repository
    from .config_environment import apply_config_environment

    config_path = getattr(args, "config", None) or os.getenv("QAR_CONFIG_FILE") or None
    file_cfg = RunnerConfig.from_file(config_path) if config_path else None
    apply_config_environment(file_cfg)
    corpus = (args.corpus or os.getenv("QAR_CORPUS_ROOT") or (file_cfg.corpus_root if file_cfg else None)
              or os.getcwd())
    cards_dir = (args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR")
                 or (file_cfg.context_cards_dir if file_cfg else None) or os.path.join(corpus, ".quest-context"))
    repo, _, _ = build_card_repository(file_cfg or RunnerConfig(), cards_dir)
    return os.path.expanduser(cards_dir), os.path.expanduser(corpus), repo


def run_cards_command(args, log) -> int:
    """Run the `cards` subcommand; returns the process exit code."""
    from .adapters import card_limit
    from .adapters.card_limit_report import report_for, write_report

    cards_dir, corpus, repo = lane_store(args)
    op = getattr(args, "cards_op", "report")
    undo = getattr(args, "undo", None)
    if repo is not None:
        store = repository_store(cards_dir, corpus, repo)
        if op == "tidy" and undo:
            return undo_repository(repo, Path(cards_dir), Path(undo))
        if op == "tidy":
            return tidy_repository(store, repo, Path(cards_dir), apply=bool(getattr(args, "apply", False)),
                                   merge_topics=bool(getattr(args, "merge_topics", False)))
        text = report_for(cards_dir, card_limit.max_cards_from_env(), cards=store._own_cards())
        print(text)
        return 0
    if not os.path.isdir(cards_dir):
        print(f"error: {cards_dir} is not a card store (a folder of card files). Pass --cards-dir.")
        return 2
    if op == "tidy":
        if undo:
            from .adapters.card_repository import FilesystemCardRepository
            from .adapters.card_store_tidy import MANIFEST_SUFFIX
            if str(undo).endswith(MANIFEST_SUFFIX):
                return undo_repository(FilesystemCardRepository(cards_dir), Path(cards_dir), Path(undo))
            return undo_cards(Path(cards_dir), Path(undo))
        if getattr(args, "merge_topics", False):
            from .adapters.card_repository import FilesystemCardRepository
            local = FilesystemCardRepository(cards_dir)
            return tidy_repository(repository_store(cards_dir, corpus, None), local, Path(cards_dir),
                                   apply=bool(getattr(args, "apply", False)), merge_topics=True,
                                   reasons=("surplus",))
        return tidy_cards(Path(cards_dir), apply=bool(getattr(args, "apply", False)))
    text = report_for(cards_dir, card_limit.max_cards_from_env())
    print(text)
    if args.write:
        from .adapters.card_limit_report import REPORT_NAME
        if write_report(Path(cards_dir), text):
            print(f"Written to {Path(cards_dir) / REPORT_NAME}")
    return 0


def repository_store(cards_dir: str, corpus: str, repo):
    """A card store over ``repo`` that never indexes anything: it is asked which cards this lane owns. ``None`` is
    the plain local folder, whose every card is this lane's."""
    from .adapters import card_limit
    from .adapters.file_context_store import FileContextStore
    return FileContextStore(cards_dir, repo_root=corpus, auto_bootstrap=False, card_repository=repo,
                            mount_mode="copy", max_cards=card_limit.max_cards_from_env())


def tidy_repository(store, repo, cards_dir: Path, *, apply: bool, merge_topics: bool = False,
                    reasons=None) -> int:
    """The full tidy of a store on the Quest backend or Qdrant: a dry run, or with ``apply`` an export, then the
    changes through the repository, then a check. Only the cards this lane owns are looked at. ``merge_topics`` joins
    cards that pin a keeper's files under another name onto it (the default keeps them as distinct topics);
    ``reasons`` narrows what is looked for (a local folder with ``--merge-topics`` looks for that alone)."""
    from .adapters import card_store_tidy as tidy

    cards = store._own_cards()
    plan = tidy.plan_store(cards, cards_dir=cards_dir, reasons=reasons or tidy.DEFAULT_REASONS,
                           own_store_id=store.own_store_id(), repo=repo, distinct_topics=not merge_topics)
    print(f"store           : {type(repo).__name__} (the cards this lane owns)")
    print(f"cards looked at : {plan.scanned:,}")
    print(f"would remove    : {len(plan.entries):,}" + (f"  ({plan.summary()})" if plan.entries else ""))
    if plan.skipped:
        print("left in place on purpose:")
        for why, count in sorted(plan.skipped.items()):
            print(f"  {count:>8,}  {why}")
    if not apply:
        print("This was a dry run, nothing changed. Add --apply to tidy: every card it touches is exported to "
              f"{tidy.state_dir_for(cards_dir)} first, and --undo with the manifest it prints puts them back.")
        return 0
    if not plan.entries:
        return 0
    run = tidy.RepositoryTidy(repo, cards_dir, batch_pause=0.2).apply(plan, cards)
    print(f"removed         : {run.removed:,}   keepers updated: {run.updated:,}")
    for why, count in sorted(run.skipped.items()):
        print(f"  {count:>8,}  skipped: {why}")
    if run.export:
        print(f"export          : {run.export}")
        print(f"undo            : quest-ai-runner cards tidy --undo {run.manifest}")
    if run.failed_checks:
        print(f"checks failed   : {len(run.failed_checks):,} (each card and why it stays are in the manifest)")
    for problem in run.problems[:10]:
        print(f"  check failed: {problem}")
    if run.failed:
        print(f"stopped         : {run.failed}")
        return 1
    return 0


def undo_repository(repo, cards_dir: Path, manifest: Path) -> int:
    """Take back a tidy made through the repository: re-create each removed card, restore each updated keeper."""
    from .adapters.card_store_tidy import RepositoryTidy

    if not manifest.is_file():
        print(f"error: {manifest} is not a file. The manifests are the *-store-tidy-manifest.jsonl files in "
              f"{cards_dir / 'tidy-state'}.")
        return 2
    result = RepositoryTidy(repo, cards_dir).undo(manifest)
    print(f"put back        : {result.restored:,}")
    print(f"keepers restored: {result.reverted:,}")
    for why, count in sorted(result.skipped.items()):
        print(f"  {count:>8,}  skipped: {why}")
    if result.failed:
        print(f"error: {result.failed}")
        return 1
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
