"""``quest-ai-runner context <move-root|migrate-imports|merge-into|store-info|upload>``: card-store maintenance.

Every command here is a DRY RUN unless ``--apply`` is given, and prints what it would change.
None of them embeds anything: they touch ``bootstrap_meta.json`` and (migrate-imports, merge-into) the
card files and vector points of the stores you point them at; ``upload`` copies a local store's cards
and existing vectors to the Quest backend (read-only on the local store). See
``docs/context-card-stores.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

STORE_COMMANDS = ("move-root", "migrate-imports", "merge-into", "store-info", "upload")
# A bulk batch whose cards carry no vector is embedded by the backend before it answers, which takes
# far longer than the card client's interactive default; a client that gives up first leaves the
# server still embedding while it sends the next batch.
UPLOAD_TIMEOUT_SECONDS = 600.0
DEFAULT_COLLECTION_PREFIX = "qar_ctx"


def is_store_command(argv: Optional[List[str]]) -> bool:
    return bool(argv) and len(argv) > 1 and argv[0] == "context" and argv[1] in STORE_COMMANDS


def open_vector_store(url: Optional[str], path: Optional[str], prefix: str,
                      vector_size: Optional[int] = None):
    """A QdrantVectorStore used only to list, relabel and delete points (no embedding happens).

    The collection is ``<prefix>_default_<size>``, so the size has to be right: it is ``vector_size``
    when given, else the one size the prefix has on that backend. Several sizes means the caller
    must say which (``SystemExit``).
    """
    if not url and not path:
        return None
    from .adapters.qdrant_vector_store import QdrantVectorStore
    store = QdrantVectorStore(url=url or None, path=path or None, collection_prefix=prefix,
                              embedder=lambda texts: [[0.0] for _ in texts], vector_size=vector_size or 1)
    if not vector_size:
        sizes = store.existing_sizes()
        if len(sizes) == 1:
            store.use_vector_size(sizes[0])
        elif len(sizes) > 1:
            raise SystemExit(f"several vector sizes exist under prefix {prefix!r} ({sorted(sizes)}); "
                             "pass --vector-size")
    return store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quest-ai-runner context")
    sub = parser.add_subparsers(dest="op", required=True)

    mv = sub.add_parser("move-root", help="record a new corpus root for a store (no re-embedding)")
    mv.add_argument("--cards-dir", required=True, help="the store's card directory")
    mv.add_argument("--new-root", required=True, help="the directory the store's relative paths now resolve from")
    mv.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    mv.add_argument("--force", action="store_true", help="apply even if the plan reports problems")

    mi = sub.add_parser("migrate-imports",
                        help="convert namespaced copy-imports to read-only mounts; list orphan points")
    mi.add_argument("--cards-dir", required=True)
    mi.add_argument("--root", default=None, help="the store's corpus root (default: its recorded root)")
    mi.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    mi.add_argument("--prune-points", action="store_true",
                    help="with --apply and a vector store, also delete the orphan points")
    mi.add_argument("--qdrant-url", default=None)
    mi.add_argument("--qdrant-path", default=None)
    mi.add_argument("--collection-prefix", default="qar_ctx")
    mi.add_argument("--vector-size", type=int, default=None,
                    help="vector size of the collection (default: detected)")

    mg = sub.add_parser("merge-into",
                        help="merge a store into a wider store, moving cards and vectors (no re-embedding)")
    mg.add_argument("--cards-dir", required=True, help="the narrow store to merge (the source)")
    mg.add_argument("--into", required=True, help="the wide store's card directory (the target)")
    mg.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    mg.add_argument("--prune-source", action="store_true",
                    help="with --apply: remove the moved cards from the source and mark it merged "
                         "(default: leave the source untouched)")
    mg.add_argument("--qdrant-url", default=None)
    mg.add_argument("--qdrant-path", default=None)
    mg.add_argument("--collection-prefix", default="qar_ctx")
    mg.add_argument("--vector-size", type=int, default=None,
                    help="vector size of the collection (default: detected)")

    si = sub.add_parser("store-info", help="show a store's identity, root, mounts and readability")
    si.add_argument("--cards-dir", required=True)
    si.add_argument("--init", action="store_true", help="give the store a store_id if it has none")
    si.add_argument("--vector-space", default=None,
                    help="declare which vector server/collection holds this store's points")
    si.add_argument("--vector-location", default=None, metavar="URL|embedded",
                    help="publish where this store's card points live (a Qdrant server URL, or "
                         "'embedded'); readers on other backends query them read-only from there")
    si.add_argument("--vector-collection-prefix", default=DEFAULT_COLLECTION_PREFIX)
    si.add_argument("--vector-size", type=int, default=None,
                    help="size of the vectors at that location (default: detected on a server)")
    si.add_argument("--embedder-id", default=None,
                    help="embedder family that produced the vectors (default: from QAR_EMBEDDER_BACKEND)")
    si.add_argument("--vector-path", default=None,
                    help="for 'embedded': the Qdrant directory (default: <cards-dir>/qdrant)")
    si.add_argument("--apply", action="store_true",
                    help="write --init / --vector-space / --vector-location (default: dry run)")
    si.add_argument("--json", action="store_true", dest="as_json")

    up = sub.add_parser("upload",
                        help="upload a local store's cards (and existing vectors) to the Quest backend")
    up.add_argument("--cards-dir", required=True, help="the local store to upload")
    scope = up.add_mutually_exclusive_group(required=True)
    scope.add_argument("--team-id", default=None, help="assign every uploaded card to this team")
    scope.add_argument("--org-id", default=None,
                       help="assign every uploaded card to this org (readable by every org member)")
    scope.add_argument("--private", action="store_true",
                       help="upload as private cards of the key's user (no team)")
    up.add_argument("--base-url", default=None,
                    help="Quest backend (default: QAR_QUEST_API_URL, else QUEST_BASE_URL)")
    up.add_argument("--api-key", default=None,
                    help="Quest key (default: QAR_QUEST_API_KEY, else QUEST_API_KEY); never printed")
    up.add_argument("--qdrant-url", default=None, help="the store's Qdrant server, to reuse its vectors")
    up.add_argument("--qdrant-path", default=None, help="the store's embedded Qdrant directory")
    up.add_argument("--collection-prefix", default="qar_ctx")
    up.add_argument("--vector-size", type=int, default=None,
                    help="vector size of the store's collection (default: detected)")
    up.add_argument("--embedder-id", default=None,
                    help="embedder that produced the store's vectors (default: the store's recorded "
                         "vector_location embedder, else QAR_EMBEDDER_BACKEND)")
    up.add_argument("--adopt-ancestor-copies", action="store_true",
                    help="upload copy-imports from a store ABOVE this root (imported_from starting "
                         "'..') as this store's own cards; nested imports stay skipped")
    up.add_argument("--apply", action="store_true", help="upload (default: dry run)")
    return parser


def run_upload(args) -> int:
    """``context upload``: plan, then (with ``--apply``) send. Exit 1 on any problem or failed card."""
    from hubofhubs.recall import card_store_identity as ident
    from .adapters.card_store_upload import apply_upload, plan_upload
    from .adapters.qdrant_vector_store import embedder_id_from_env
    from .adapters.quest_api_card_repository import (
        QuestApiCardRepository, QuestCardApiError, quest_card_settings_from_env)

    settings = quest_card_settings_from_env()
    base_url = args.base_url or settings["base_url"]
    api_key = args.api_key or settings["api_key"]
    if not base_url or not api_key:
        print("no Quest credentials: pass --base-url/--api-key or set QAR_QUEST_API_URL|QUEST_BASE_URL "
              "and QAR_QUEST_API_KEY|QUEST_API_KEY", file=sys.stderr)
        return 1
    org_id = "" if args.private else (args.org_id or "").strip()
    team_id = "" if (args.private or org_id) else (args.team_id or "").strip()
    repo = QuestApiCardRepository(base_url=base_url, api_key=api_key, team_id=team_id, org_id=org_id,
                                  timeout=UPLOAD_TIMEOUT_SECONDS)
    try:
        user = repo.resolve_user_id()
    except Exception as exc:  # noqa: BLE001
        print(f"could not identify the key's user: {exc}", file=sys.stderr)
        return 1
    if not user:
        print("the Quest backend did not identify the key's user (whoami failed); see the error above",
              file=sys.stderr)
        return 1
    try:
        backend_embedder = repo.embedder_info()
    except QuestCardApiError:
        backend_embedder = None
    vector = open_vector_store(args.qdrant_url, args.qdrant_path, args.collection_prefix, args.vector_size)
    meta = ident.read_meta(args.cards_dir)
    recorded = ident.vector_location_of(meta) or {}
    local_embedder = args.embedder_id if args.embedder_id is not None else (
        str(recorded.get("embedder") or "") or embedder_id_from_env())
    plan = plan_upload(args.cards_dir, team_id=team_id, org_id=org_id,
                       adopt_ancestor_copies=args.adopt_ancestor_copies, backend_embedder=backend_embedder,
                       vector_store=vector, local_embedder_id=local_embedder)
    print(plan.render())
    if plan.problems:
        return 1
    if not args.apply:
        print("dry run: nothing was uploaded; pass --apply to upload")
        return 0

    def show(number: int, reply: dict) -> None:
        if "error" in reply:
            print(f"  batch {number}: FAILED {reply['error']}")
        else:
            print(f"  batch {number}: {reply.get('counts')}")

    result = apply_upload(plan, repo, on_batch=show)
    print(f"uploaded in {result['batches']} batch(es): {result['counts']}")
    if result["failed"]:
        print(f"FAILED cards ({len(result['failed'])}): {', '.join(result['failed'][:20])}", file=sys.stderr)
        return 1
    print("APPLIED")
    return 0


def vector_location_from_args(args):
    """``(record, problem)`` for ``store-info --vector-location``; the size is detected on a server."""
    from hubofhubs.recall import card_store_identity as ident
    from .adapters.qdrant_vector_store import embedder_id_from_env
    location = (args.vector_location or "").strip()
    if not location:
        return None, "--vector-location needs a server URL or 'embedded'"
    size = args.vector_size
    prefix = args.vector_collection_prefix or DEFAULT_COLLECTION_PREFIX     # an explicit empty value means the default, as it always did
    record = ident.vector_location_record(
        location, collection_prefix=prefix, vector_size=size,
        embedder=args.embedder_id if args.embedder_id is not None else embedder_id_from_env())
    if record["location"] == ident.EMBEDDED:
        import os
        record["path"] = os.path.abspath(args.vector_path or os.path.join(args.cards_dir, "qdrant"))
    elif not size:
        from qdrant_client import QdrantClient
        from .adapters.card_store_vectors import ReadOnlyVectorReader
        try:
            sizes = ReadOnlyVectorReader(QdrantClient(url=location, timeout=5),
                                         collection_prefix=prefix).detect_sizes()
        except Exception as exc:  # noqa: BLE001
            return None, f"could not reach {location} to detect the vector size ({exc.__class__.__name__}); pass --vector-size"
        if len(sizes) != 1:
            return None, (f"found vector sizes {sorted(sizes) or 'none'} under prefix "
                          f"{prefix!r}; pass --vector-size")
        record["vector_size"] = sizes[0]
    return record, ""


def run(argv: List[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    from hubofhubs.recall import card_store_identity as ident
    from .adapters import card_store_migrate as mounts

    if args.op == "upload":
        return run_upload(args)

    if args.op == "move-root":
        plan = (ident.apply_move_root(args.cards_dir, args.new_root, force=args.force)
                if args.apply else ident.plan_move_root(args.cards_dir, args.new_root))
        print(plan.render())
        print("APPLIED" if args.apply and plan.ok else "dry run: nothing was changed")
        return 0 if plan.ok or (args.apply and args.force) else 1

    if args.op == "migrate-imports":
        vector = open_vector_store(args.qdrant_url, args.qdrant_path, args.collection_prefix,
                                   args.vector_size)
        plan = mounts.plan_migrate_imports(args.cards_dir, args.root, vector_store=vector)
        if args.apply and not plan.problems:
            plan = mounts.apply_migrate_imports(plan, prune_points=args.prune_points,
                                                vector_store=vector)
        print(plan.render())
        if plan.pruned_points:
            print(f"  pruned {plan.pruned_points} orphan vector point(s)")
        return 0 if not plan.problems else 1

    if args.op == "merge-into":
        from .adapters import card_store_merge as merge
        vector = open_vector_store(args.qdrant_url, args.qdrant_path, args.collection_prefix,
                                   args.vector_size)
        if args.apply:
            plan = merge.apply_merge(args.cards_dir, args.into, vector_store=vector,
                                     prune_source_after=args.prune_source)
        else:
            plan = merge.plan_merge(args.cards_dir, args.into, vector_store=vector,
                                    prune_source=args.prune_source)
        print(plan.render())
        return 0 if plan.ok else 1

    if args.op == "store-info":
        meta = ident.read_meta(args.cards_dir)
        if not meta:
            print("no bootstrap_meta.json here: this directory is not a bootstrapped store "
                  "(run a bootstrap first)", file=sys.stderr)
            return 1
        changes = {}
        if args.init and not meta.get("store_id"):
            changes["store_id"] = "(new)"
        if args.vector_space is not None:
            changes["vector_space"] = args.vector_space
        location_record = None
        if args.vector_location is not None:
            location_record, problem = vector_location_from_args(args)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            changes["vector_location"] = location_record
        if args.apply and changes:
            ident.ensure_store_id(args.cards_dir)
            if args.vector_space is not None:
                ident.update_identity(args.cards_dir, vector_space=args.vector_space)
            if location_record is not None:
                ident.update_identity(args.cards_dir, vector_location=location_record)
            meta = ident.read_meta(args.cards_dir)
        info = {
            "store_id": meta.get("store_id"),
            "root": meta.get("root"),
            "vector_id_scheme": meta.get("vector_id_scheme"),
            "vector_space": meta.get("vector_space"),
            "vector_location": meta.get("vector_location"),
            "merged_from": meta.get("merged_from"),
            "merged_into": meta.get("merged_into"),
            "readable_by_this_user": ident.check_readable(args.cards_dir).reason,
            "mounts": [{**m, "readable": ident.check_readable(m.get("cards_dir", "")).reason}
                       for m in meta.get("mounts") or [] if isinstance(m, dict)],
            "pending_changes": changes if not args.apply else {},
        }
        if args.as_json:
            print(json.dumps(info, indent=2))
        else:
            for k, v in info.items():
                print(f"{k}: {v}")
            if changes and not args.apply:
                print("dry run: pass --apply to write the pending changes")
        return 0
    return 2
