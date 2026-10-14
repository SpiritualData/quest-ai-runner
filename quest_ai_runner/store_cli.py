"""``quest-ai-runner context <move-root|migrate-imports|store-info>``: card-store maintenance.

Every command here is a DRY RUN unless ``--apply`` is given, and prints what it would change.
None of them embeds anything: they touch ``bootstrap_meta.json`` and (migrate-imports) the card
files and orphan vector points of the store you point them at. See ``docs/context-card-stores.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

STORE_COMMANDS = ("move-root", "migrate-imports", "store-info")


def is_store_command(argv: Optional[List[str]]) -> bool:
    return bool(argv) and len(argv) > 1 and argv[0] == "context" and argv[1] in STORE_COMMANDS


def open_vector_store(url: Optional[str], path: Optional[str], prefix: str):
    """A QdrantVectorStore used only to list/delete points (no embedding happens)."""
    if not url and not path:
        return None
    from .adapters.qdrant_vector_store import QdrantVectorStore
    return QdrantVectorStore(url=url or None, path=path or None, collection_prefix=prefix,
                             embedder=lambda texts: [[0.0] for _ in texts], vector_size=1)


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

    si = sub.add_parser("store-info", help="show a store's identity, root, mounts and readability")
    si.add_argument("--cards-dir", required=True)
    si.add_argument("--init", action="store_true", help="give the store a store_id if it has none")
    si.add_argument("--vector-space", default=None,
                    help="declare which vector server/collection holds this store's points")
    si.add_argument("--apply", action="store_true", help="write --init / --vector-space (default: dry run)")
    si.add_argument("--json", action="store_true", dest="as_json")
    return parser


def run(argv: List[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    from .adapters import card_store_identity as ident
    from .adapters import card_store_mounts as mounts

    if args.op == "move-root":
        plan = (ident.apply_move_root(args.cards_dir, args.new_root, force=args.force)
                if args.apply else ident.plan_move_root(args.cards_dir, args.new_root))
        print(plan.render())
        print("APPLIED" if args.apply and plan.ok else "dry run: nothing was changed")
        return 0 if plan.ok or (args.apply and args.force) else 1

    if args.op == "migrate-imports":
        vector = open_vector_store(args.qdrant_url, args.qdrant_path, args.collection_prefix)
        plan = mounts.plan_migrate_imports(args.cards_dir, args.root, vector_store=vector)
        if args.apply and not plan.problems:
            plan = mounts.apply_migrate_imports(plan, prune_points=args.prune_points,
                                                vector_store=vector)
        print(plan.render())
        if plan.pruned_points:
            print(f"  pruned {plan.pruned_points} orphan vector point(s)")
        return 0 if not plan.problems else 1

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
        if args.apply and changes:
            ident.ensure_store_id(args.cards_dir)
            if args.vector_space is not None:
                ident.update_identity(args.cards_dir, vector_space=args.vector_space)
            meta = ident.read_meta(args.cards_dir)
        info = {
            "store_id": meta.get("store_id"),
            "root": meta.get("root"),
            "vector_id_scheme": meta.get("vector_id_scheme"),
            "vector_space": meta.get("vector_space"),
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
