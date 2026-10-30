"""Spec selection for the repository a run is working in, ready for day-to-day use.

``spec_selection`` is the algorithm over an index it is handed; this module is what a run calls: it finds the repository's ESOI
records from the working folder, keeps their index in a cache that refreshes by itself when a record file changes (the build is
paid once per edit, not once per task), adds a local vector search when ``fastembed`` is installed (never required), and
answers the three questions a run asks about the specifications: which ones does this request concern (``find``), what is
connected to this one (``related``), and what else is in this intent (``intent``). Reading one in full (``show``) goes through the
ESOI library, so the index never holds a second copy of a record.

Command line (no model, free):  python -m quest_ai_runner.core.repo_specs find|related|intent|show ...
The tools ``find_specs``, ``related_specs``, ``intent_specs`` and ``show_spec`` (``tools_repo_specs``) call the same functions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .runtime_settings import process_environment
from .spec_selection import (SpecSelection, VectorSearch, card_line, intent_specs, related_specs, select_specs)
from .unified_search import RecordView, TagTable, UnifiedSelection, solutions_and_problems_with_links, views_from_spec_index

log = logging.getLogger("quest-ai-runner.repo-specs")
CONFIG_NAME = "esoi.toml"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"


def find_root(start: Optional[Path] = None) -> Optional[Path]:
    """The repository whose specifications apply: ``QAR_SPEC_ROOT`` when set, else the nearest folder at or above ``start``
    (default: the working folder) that holds an ``esoi.toml``. None when there is none (then no spec tool is offered)."""
    env = os.environ.get("QAR_SPEC_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve() if (Path(env).expanduser() / CONFIG_NAME).is_file() else None
    here = Path(start or Path.cwd()).resolve()
    return next((p for p in [here, *here.parents] if (p / CONFIG_NAME).is_file()), None)


def root_for(working_dir: Optional[str]) -> Optional[Path]:
    """``find_root`` from a folder given as text; no folder means the process's own (a tool call runs in the work folder)."""
    return find_root(Path(working_dir) if working_dir else None)


def slice_root(working_dir: Optional[str]) -> Optional[Path]:
    """The repository a task's slice is read from: None with no folder to look from (the process's own folder is a guess about
    where the work happens), else ``find_root`` (so ``QAR_SPEC_ROOT`` still names the repository)."""
    if not (working_dir or process_environment().get("QAR_SPEC_ROOT")):
        return None
    return root_for(working_dir)


def cache_dir() -> Path:
    return Path(os.environ.get("QAR_SPEC_CACHE") or Path.home() / ".cache" / "quest-ai-runner" / "spec_index")


def repo_index(root: Path) -> Dict[str, Any]:
    """The repository's specification index, from the cache when its record files have not changed (``esoi.spec_index.cached``)."""
    from esoi import config as esoi_config, spec_index
    config = esoi_config.load(Path(root))
    return spec_index.cached(config, cache_dir() / (hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12] + ".json"))


def local_vector_search(index: Dict[str, Any]) -> Optional[VectorSearch]:
    """``vector_search(text, k) -> [(id, cosine)]`` over local bge-small embeddings of the index's card lines, or None when
    ``fastembed`` or its model is not available (selection then runs on the lexical search alone). The embeddings of the entries
    are cached next to the index, keyed by its digest."""
    try:
        import numpy as np
        from fastembed import TextEmbedding
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"))
    except Exception:  # noqa: BLE001  no embedder is a normal setup, not an error
        log.debug("no local embedder for spec selection", exc_info=True)
        return None
    entries = index["entries"]
    store = cache_dir() / f"vectors_{index['digest']}.npy"
    try:
        matrix = np.load(store) if store.is_file() else None
        if matrix is None or matrix.shape[0] != len(entries):
            raise ValueError("stale")
    except Exception:  # noqa: BLE001
        matrix = np.array([[float(x) for x in v] for v in model.embed([card_line(e) for e in entries])])
        matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            np.save(store, matrix)
        except OSError:
            log.debug("spec vectors not cached", exc_info=True)
    ids = [e["id"] for e in entries]

    def search(text: str, k: int) -> List[Tuple[str, float]]:
        q = np.array([float(x) for x in next(iter(model.embed([text])))])
        scores = matrix @ (q / np.linalg.norm(q))
        return [(ids[i], float(scores[i])) for i in np.argsort(-scores)[:k]]
    return search


def find(task: str, root: Optional[Path] = None, *, provider: Any = None, model: Optional[str] = None,
         expand: int = 1) -> Tuple[SpecSelection, Dict[str, Any]]:
    """The one entry point: the specifications of the working repository that ``task`` concerns, and the index they came
    from. With a ``provider`` an ambiguous request costs one cheap call; without one it is free and returns the best three
    candidates of the ambiguous ones."""
    root = root or find_root()
    if root is None:
        raise LookupError("no esoi.toml above the working folder (set QAR_SPEC_ROOT)")
    index = repo_index(root)
    return select_specs(task, index, provider=provider, model=model, vector_search=local_vector_search(index), expand=expand), index


def repo_tag_table(root: Path) -> TagTable:
    """``esoi/tags.csv`` and the solutions' ``tags`` column, read from the record files (never copied). Empty when absent."""
    from esoi import config as esoi_config
    folder = Path(root) / esoi_config.load(Path(root)).structure_dir

    def rows(name: str) -> List[Dict[str, str]]:
        try:
            with open(folder / name, newline="", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))
        except OSError:
            return []

    def split(value: Optional[str]) -> Tuple[str, ...]:
        return tuple(p.strip() for p in (value or "").split(";") if p.strip())

    def labels(cell: Optional[str]) -> Tuple[str, ...]:
        """A solution's ``tags`` cell is a JSON list of {tag, risk_ids}; the esoi library reads it (older ``;`` labels also read)."""
        try:
            from esoi.fields import parse_tags
            return tuple(t["tag"] for t in parse_tags(cell) if t["tag"])
        except ImportError:   # a library that predates esoi.fields
            return split(cell)
    return TagTable(tag_problems={r["tag"].strip(): split(r.get("problem_ids")) for r in rows("tags.csv") if (r.get("tag") or "").strip()},
                    solution_tags={r["solution_id"]: labels(r.get("tags")) for r in rows("solutions.csv") if r.get("solution_id")})


def search(task: str, root: Optional[Path] = None, *, cards: List[RecordView] = (), provider: Any = None,
           model: Optional[str] = None, tags: Optional[List[str]] = None, tag_mode: str = "boost",
           max_ranked: int = 5, expand: int = 3) -> UnifiedSelection:
    """Cards and the working repository's records in one ranking (``unified_search``, not selected anywhere): ``cards`` are
    views a caller already has (``adapters.card_views.card_views``); the records are views over the cached index. The local
    vector search embeds the records only, so it is used only when there are no cards: a stage that sees one source would
    rank that source above the other in the fusion. With cards the ranking is lexical, then the gate and the model."""
    root = root or find_root()
    if root is None:
        raise LookupError("no esoi.toml above the working folder (set QAR_SPEC_ROOT)")
    index = repo_index(root)
    table = repo_tag_table(root) if tags else None
    views = list(cards) + views_from_spec_index(index, table)
    vector = None if cards else local_vector_search(index)
    return solutions_and_problems_with_links(task, views, provider=provider, model=model, vector_search=vector,
                                             tags=tags, tag_table=table, tag_mode=tag_mode, max_ranked=max_ranked, expand=expand)


def render(index: Dict[str, Any], ids: List[str], linked: Optional[Dict[str, str]] = None) -> str:
    """Tier 1 lines of the chosen records (kind id: title, summary), linked ones marked, ready for a prompt."""
    by_id = {e["id"]: e for e in index["entries"]}
    return "\n".join(f"- {card_line(by_id[i])}" + (f"  (linked from {linked[i]})" if linked and i in linked else "")
                     for i in ids if i in by_id)


def show(root: Path, entry_id: str, tier: int = 2) -> str:
    """One record at a tier (0 line, 1 plus summary and links, 2 the full record), through the ESOI library."""
    from esoi import config as esoi_config, spec_index
    config = esoi_config.load(Path(root))
    return spec_index.show(config, repo_index(root), entry_id, tier)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m quest_ai_runner.core.repo_specs", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["find", "related", "intent", "show"])
    ap.add_argument("target", nargs="?", default="", help="find: the request; related and show: a record id; intent: an intent id (blank = all)")
    ap.add_argument("--root", default=None)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--tier", type=int, default=2, choices=[0, 1, 2])
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else find_root()
    if root is None:
        print("no esoi.toml above the working folder (set QAR_SPEC_ROOT or pass --root)", file=sys.stderr)
        return 2
    index = repo_index(root)
    try:
        if args.action == "find":
            got = select_specs(args.target, index, vector_search=local_vector_search(index))
            print(f"route: {got.route}\n" + (render(index, got.ids, got.linked) or "(no specification concerns this)"))
        elif args.action == "related":
            print("\n".join(f"- {h['kind']} {h['id']}: {h['title']}  (step {h['depth']} via {h['via']})"
                            for h in related_specs(index, args.target, args.depth)))
        elif args.action == "intent":
            print("\n".join(f"- {h['kind']} {h['id']}: {h['title']}" for h in intent_specs(index, args.target)))
        else:
            print(show(root, args.target, args.tier))
    except KeyError:
        print(f"no record {args.target!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
