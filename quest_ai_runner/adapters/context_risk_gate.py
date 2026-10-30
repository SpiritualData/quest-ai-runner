"""The risk gate: one decision, every card source.

A folder can be dangerous to index for a reason a file count never shows. The case that motivated
this: a machine's daily backup snapshots (one folder per date, each a copy of the whole tree) each
carry their own card store, so one backups folder held 155 stores and 760,608 cards, and a corpus
rooted above it would have walked, imported and mounted every one of them.

``folder_gate`` is the single function every card source asks (the file walk, nested store
discovery, ancestor imports, mounts discovery and the migrate-imports report), so there is one
answer and no per-path copy of it:

* Cheap structural signals, no model (``risk_signals``, a pure function): a folder whose subtree
  holds three or more card stores (one legitimate nested store is normal and is no signal); a
  series of sibling folders named like dates, timestamps or versions that share one child
  structure; a very large folder (the folder review's own size floor); and a COPY OF THE CORPUS
  ITSELF: a folder with a card store beneath it that is a near-complete copy of this corpus, even
  when it is the only one (a lone backup snapshot is a series of one). The copy signal is any of:
  the nested store carries this store's own ``store_id``; its recorded root is this corpus's root
  while it sits elsewhere under a dated or numbered folder name, or beside a repeated layout (a
  store copied along with the tree it indexes); or its folder repeats at least three of the corpus
  root's own top-level folder names (80% of them, not counting the folders on the way down to it).
  That last test is asked of EVERY folder the walk meets, with or without a card store, because a
  backup snapshot taken without its store is still a copy. One ordinary nested store
  with none of these is normal and stays imported.
* A folder with a signal is ALWAYS put to the folder review, whatever the corpus size, with the
  signal as context. The verdict is cached in ``folder_review.json`` with ``source: risk_signal``
  and stays human-editable: ``"index": true`` on the folder overrides a skip.
* If the review cannot run (switched off, no provider, a failed call), a folder with a HARD signal
  (stores, series or copy) is not indexed, not descended into, and no store beneath it is imported or
  mounted. One warning per run names it and the override. Folders without a signal keep the
  fail-open behaviour: an unreviewable ordinary corpus is indexed in full.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from hubofhubs.recall.walk import prune_dirnames, skip_subtrees
from .card_store_identity import BOOTSTRAP_META_FILE, is_merged, read_meta
from .context_bootstrap_support import _log
from .context_folder_review import (
    _FOLDER_REVIEW_FILE,
    _FOLDER_REVIEW_MIN_FILES,
    _FOLDER_REVIEW_START_DEPTH,
    HARD_RISK_KINDS,
    _dir_file_counts,
    _folder_review_enabled,
    _is_excluded_folder,
    _roll_up_to_depth,
    load_verdicts,
    review_corpus,
)

_CARDS_DIR = ".quest-context"

# Three card stores under one folder is a pattern (copies of a tree), one is a normal nested
# corpus, two can be a coincidence. Three dated siblings is a series, two is a pair.
RISK_MIN_STORES = 3
RISK_MIN_SERIES = 3
# Two snapshot folders are "the same structure" when their child names overlap at least this much.
_SERIES_MIN_SIMILARITY = 0.6
_SERIES_MAX_CANDIDATES = 500
_SERIES_MAX_CHILD_NAMES = 60
_MAX_ANCESTOR_LEVELS = 12

# Folder NAMES that mark a copy in a series. Names only, never model output. After optional word
# prefixes joined by - _ . (each up to 12 letters or digits, starting with a letter: ``hq-docs-``,
# ``hq_backup_``, ``snapshot-``) the name ends in one of:
#   a date, compact or ISO, with an optional time suffix: 2026-09-10, 20260910_0230,
#     2026-09-30-0230, 2026-09-10T02-30, 2026.09.10 02:30:15
#   a unix timestamp of 9 to 13 digits
#   a version number with 2 to 4 parts: v1.2, 3.0.1, 2.1.0-rc1
# So ``hq-docs-2026-09-30-0230`` and ``hq_backup_20260930`` read as serial; ``notes-2026``, ``v2``
# and ``backup`` do not.
_SERIAL_NAME = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9]{0,11}[-_.])*(?:"
    r"\d{4}[-_.]?\d{2}[-_.]?\d{2}(?:[-_.T ]?\d{2}[-_.:]?\d{2}(?:[-_.:]?\d{2})?)?"
    r"|\d{9,13}"
    r"|[vV]?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?"
    r")$")
_SERIAL_NAME_MAX = 80

Signals = Dict[str, List[Tuple[str, str]]]


def name_looks_serial(name: str) -> bool:
    """Whether a folder name reads as a date, timestamp or version number."""
    return len(name or "") <= _SERIAL_NAME_MAX and bool(_SERIAL_NAME.match(name or ""))


def _similar(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= _SERIES_MIN_SIMILARITY


def find_series(names: Sequence[str], child_names_of: Callable[[str], Sequence[str]],
                min_members: int = RISK_MIN_SERIES) -> List[str]:
    """The largest group of date/version-named siblings that share one child structure.

    ``names`` are sibling folder names, ``child_names_of(name)`` the names inside one of them.
    Returns the sorted member names, or [] when no group reaches ``min_members``. Empty folders
    never count: nothing is shared by emptiness.
    """
    shapes: Dict[str, frozenset] = {}
    for name in [n for n in names if name_looks_serial(n)][:_SERIES_MAX_CANDIDATES]:
        shape = frozenset(list(child_names_of(name))[:_SERIES_MAX_CHILD_NAMES])
        if shape:
            shapes[name] = shape
    best: List[str] = []
    for pivot, shape in shapes.items():
        group = [n for n, other in shapes.items() if n == pivot or _similar(shape, other)]
        if len(group) > len(best):
            best = group
    return sorted(best) if len(best) >= min_members else []


def risk_signals(stores: Sequence[str], series: Dict[str, Sequence[str]],
                 counts: Dict[str, int], copies: Optional[Dict[str, str]] = None) -> Signals:
    """Risk signals per folder, from facts already gathered. Pure: no filesystem, no model.

    ``stores`` are the corpus-relative folders that hold a card store, ``series`` maps a folder to
    the names of its dated or numbered copies, ``counts`` is indexable files per directory. The
    corpus root is never a risk folder (it cannot be excluded). A folder is reported at the
    SHALLOWEST level that holds the pattern, because a verdict there covers everything below.
    ``copies`` maps a folder to why it is a copy of the corpus itself (``scan_tree`` finds them).
    Each signal is ``(kind, text)``; ``stores``, ``series`` and ``copy`` are hard, ``large`` is
    context.
    """
    out: Signals = {}
    for folder, why in (copies or {}).items():
        if folder:
            out.setdefault(folder, []).append(("copy", f"looks like a copy of this corpus ({why})"))
    for parent, members in series.items():
        if parent and len(members) >= RISK_MIN_SERIES:
            ordered = sorted(members)
            out.setdefault(parent, []).append((
                "series", f"looks like a series of {len(ordered)} dated or numbered copies "
                          f"({ordered[0]} ... {ordered[-1]})"))
    below: Dict[str, int] = {}
    for store in stores:
        parts = [p for p in store.split("/") if p and p != "."]
        for depth in range(1, len(parts) + 1):
            key = "/".join(parts[:depth])
            below[key] = below.get(key, 0) + 1
    flagged: List[str] = []
    for folder in sorted(below, key=lambda f: (f.count("/"), f)):
        if below[folder] >= RISK_MIN_STORES and not any(folder.startswith(f + "/") for f in flagged):
            flagged.append(folder)
            out.setdefault(folder, []).append((
                "stores", f"contains {below[folder]} card stores"))
    for folder, n in _roll_up_to_depth(counts, _FOLDER_REVIEW_START_DEPTH).items():
        if folder != "." and n >= _FOLDER_REVIEW_MIN_FILES:
            out.setdefault(folder, []).append(("large", f"very large, {n} files"))
    return out


# --- gathering the facts ------------------------------------------------------------------------

@dataclass
class TreeFacts:
    stores: List[str] = field(default_factory=list)
    series: Dict[str, List[str]] = field(default_factory=dict)
    series_paths: List[Path] = field(default_factory=list)
    copies: Dict[str, str] = field(default_factory=dict)      # folder -> why it copies the corpus
    ancestor_series: Dict[str, int] = field(default_factory=dict)


def _has_store(directory: Path) -> bool:
    try:
        cards = directory / _CARDS_DIR
        return (cards / BOOTSTRAP_META_FILE).is_file() and not is_merged(str(cards))
    except OSError:
        return False


def _child_dir_names(directory: Path, skip_dirs: Set[str]) -> List[str]:
    names: List[str] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.startswith(".") or entry.name in skip_dirs:
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
                if len(names) >= _SERIES_MAX_CHILD_NAMES:
                    break
    except OSError:
        pass
    return sorted(names)


# A folder is a copy of the corpus's layout when it repeats at least this share of the root's own
# top-level folder names, and at least two of them (one shared name is a coincidence).
_COPY_MIN_SHARE = 0.6
_COPY_MIN_NAMES = 2
_COPY_STRONG_NAMES = 3
_COPY_STRONG_SHARE = 0.8


def shared_layout(rel: str, inside: Sequence[str], root_layout: Sequence[str]) -> Tuple[List[str], float, int]:
    """``(shared names, share, names compared)``: which of the corpus root's top-level folder names the folder
    ``rel`` repeats among its own child folders ``inside``, ignoring the names on the way down to it."""
    on_the_way = set(rel.split("/"))
    names = [n for n in root_layout if n not in on_the_way]
    held = set(inside)
    shared = [n for n in names if n in held]
    return shared, (len(shared) / len(names) if names else 0.0), len(names)


def layout_copy_reason(rel: str, inside: Sequence[str], root_layout: Sequence[str]) -> str:
    """Why the folder ``rel`` is a copy of the corpus by its layout alone, or "": it repeats at least three of the
    corpus root's top-level folder names and 80% of them. Structure only; asked of EVERY folder the walk meets,
    whether or not it holds a card store (a backup snapshot copied without one is still a copy). A dated or
    numbered name on its path is said too, as context for the review."""
    shared, share, _ = shared_layout(rel, inside, root_layout)
    if len(shared) < _COPY_STRONG_NAMES or share < _COPY_STRONG_SHARE:
        return ""
    dated = any(name_looks_serial(part) for part in rel.split("/"))
    return (f"it repeats the corpus's own layout ({', '.join(shared[:4])})"
            + (" under a dated or numbered name" if dated else ""))


def copy_of_self_reason(store_dir: Path, rel: str, walk_root: Path, own_cards_dir: Optional[Path],
                        root_layout: Sequence[str], skip_dirs: Set[str]) -> str:
    """Why the store at ``store_dir`` (``rel`` under the corpus) is a copy of the corpus itself, or "".

    Structure only, never names or model output: (1) the same ``store_id`` as this corpus's own
    store; (2) a recorded root equal to this corpus's root while the store sits in a sub-folder, together
    with a date/version-like folder name on its path or a repeated layout; (3) the folder repeats
    at least three of the corpus root's top-level folder names and 80% of them, ignoring the
    folders on the way down to it. A legitimate nested project matches none of these.
    """
    cards = store_dir / _CARDS_DIR
    meta = read_meta(str(cards))
    if own_cards_dir is not None:
        own_id = read_meta(str(own_cards_dir)).get("store_id")
        if own_id and meta.get("store_id") == own_id:
            return "it carries this store's own identity"
    inside = _child_dir_names(store_dir, skip_dirs)
    shared, share, _ = shared_layout(rel, inside, root_layout)
    layout = len(shared) >= _COPY_MIN_NAMES and share >= _COPY_MIN_SHARE
    recorded = meta.get("root")
    if recorded:
        try:
            same_root = Path(os.path.realpath(recorded)) == Path(os.path.realpath(walk_root))
        except OSError:
            same_root = False
        # A recorded root alone is weak (a store can be adopted or moved); it counts with a dated
        # name on the path or a repeated layout.
        if same_root and (layout or any(name_looks_serial(part) for part in rel.split("/"))):
            return "its recorded root is this corpus's root"
    # The layout alone must be strong: a nested project may share a folder name or two.
    return layout_copy_reason(rel, inside, root_layout)


def scan_tree(walk_root: Path, skip_dirs: Set[str], own_cards_dir: Optional[Path] = None) -> TreeFacts:
    """Walk the corpus once, cheaply, collecting what ``risk_signals`` needs. Never raises.

    A confirmed series is not descended into (its copies are what the walk must not enter), so
    the scan stays cheap on exactly the trees that are expensive.
    """
    facts = TreeFacts()
    try:
        root = walk_root.resolve()
        own = own_cards_dir.resolve() if own_cards_dir is not None else None
        # The corpus's own card folder is the lane's state, not its layout: a copy need not repeat it.
        root_layout = [n for n in _child_dir_names(root, skip_dirs) if own is None or root / n != own]
        for dirpath, dirnames, _files in os.walk(root):
            current = Path(dirpath)
            if own is not None and current.resolve() == own:
                dirnames[:] = []
                continue
            prune_dirnames(dirnames, current=current, base_skip=skip_dirs)
            dirnames.sort()
            rel = current.relative_to(root).as_posix()
            rel = "" if rel == "." else rel
            if rel and _has_store(current) and (own is None or (current / _CARDS_DIR).resolve() != own):
                facts.stores.append(rel)
                why = copy_of_self_reason(current, rel, root, own, root_layout, skip_dirs)
                if why:
                    facts.copies[rel] = why
                    facts.series_paths.append(current)
                    dirnames[:] = []
                    continue
            if not rel:
                continue
            # A copy of the corpus WITHOUT a card store of its own (a snapshot taken without one, or with it
            # skipped) is found by its layout alone, for every folder the walk meets.
            why = layout_copy_reason(rel, dirnames, root_layout)
            if why:
                facts.copies[rel] = why
                facts.series_paths.append(current)
                dirnames[:] = []
                continue
            members = find_series(dirnames, lambda n: _child_dir_names(current / n, skip_dirs))
            if members:
                facts.series[rel] = members
                for member in members:
                    facts.series_paths.append(current / member)
                    if _has_store(current / member):
                        facts.stores.append(f"{rel}/{member}")
                dirnames[:] = [d for d in dirnames if d not in members]
    except Exception:  # noqa: BLE001 -- a scan that fails sees no risk, which is today's behaviour
        _log.info("context index: risk scan failed; no risk signals", exc_info=True)
    return facts


def scan_ancestors(walk_root: Path, skip_dirs: Set[str]) -> Signals:
    """Signals for the folders ABOVE the corpus that hold a store sitting in a series of copies.

    Keyed by the path of the series' parent relative to the corpus root (``".."``, ``"../.."``),
    so the ancestor-import path asks the same gate. Never raises.
    """
    out: Signals = {}
    try:
        current = walk_root.resolve()
        for level in range(1, _MAX_ANCESTOR_LEVELS + 1):
            parent = current.parent
            if parent == current:
                break
            current = parent
            if not _has_store(current) or not name_looks_serial(current.name):
                continue
            siblings = _child_dir_names(current.parent, skip_dirs)
            members = find_series(siblings, lambda n: _child_dir_names(current.parent / n, skip_dirs))
            if current.name in members:
                key = "/".join([".."] * (level + 1))
                out[key] = [("series", f"holds a card store that is one of {len(members)} dated or "
                                       f"numbered copies ({members[0]} ... {members[-1]})")]
    except Exception:  # noqa: BLE001
        pass
    return out


# --- the gate -------------------------------------------------------------------------------------

@dataclass
class FolderGate:
    """The answer every card source asks. ``excluded`` holds review exclusions and risk blocks."""
    walk_root: Path
    excluded: Set[str] = field(default_factory=set)
    blocked: Dict[str, str] = field(default_factory=dict)   # unjudged risk folder -> why

    def rel_of(self, path: Path) -> Optional[str]:
        """``path`` relative to the corpus root ("" for the root), or None when outside it."""
        try:
            rel = Path(os.path.realpath(path)).relative_to(os.path.realpath(self.walk_root)).as_posix()
        except ValueError:
            return None
        return "" if rel == "." else rel

    def excludes_rel(self, rel: str) -> bool:
        return _is_excluded_folder(rel, self.excluded)

    def excludes_dir(self, path: Path) -> bool:
        """Whether a directory is, or sits under, a folder this corpus must not read."""
        rel = self.rel_of(path)
        return bool(rel) and self.excludes_rel(rel)

    def ancestor_blocked(self, ancestor_root: Path) -> bool:
        """Whether a store ABOVE the corpus sits in a series of copies the gate turned away."""
        try:
            key = os.path.relpath(os.path.realpath(ancestor_root.parent),
                                  os.path.realpath(self.walk_root)).replace(os.sep, "/")
        except ValueError:
            return False
        return key in self.excluded

    def blocked_report(self) -> List[Tuple[str, str]]:
        return sorted(self.blocked.items())


def folder_gate(walk_root: Path, skip_dirs: Set[str], provider, model: Optional[str],
                cards_dir: Path, *, persist: bool = True) -> FolderGate:
    """Decide which folders of ``walk_root`` no card source may read. Never raises.

    Replaces the bare folder review call: the exclusions it returns are the review's, plus every
    risk-signalled folder the review could not judge. ``provider`` None means no model call is
    made (cached verdicts still apply); ``persist=False`` also leaves the verdict file alone, which
    is what a read-only report wants.
    """
    gate = FolderGate(walk_root=Path(walk_root).resolve())
    try:
        facts = scan_tree(walk_root, skip_dirs, cards_dir)
        with skip_subtrees(facts.series_paths):
            counts = _dir_file_counts(walk_root, skip_dirs)
        risk = risk_signals(facts.stores, facts.series, counts, facts.copies)
        risk.update(scan_ancestors(walk_root, skip_dirs))
        verdicts: Dict[str, Dict] = load_verdicts(cards_dir)
        if _folder_review_enabled():
            try:
                gate.excluded, verdicts = review_corpus(walk_root, skip_dirs, provider, model,
                                                        cards_dir, risk=risk, counts=counts,
                                                        persist=persist)
            except Exception:  # noqa: BLE001 -- the ordinary corpus is indexed in full
                _log.info("context index: folder review failed; ordinary folders stay indexed",
                          exc_info=True)
        else:
            # Review off: the ordinary corpus is untouched, but a human's verdict on a risky
            # folder is still the override the warning promises.
            gate.excluded = {f for f in risk
                             if verdicts.get(f, {}).get("index", True) is False}
        for folder, signals in risk.items():
            if not any(kind in HARD_RISK_KINDS for kind, _ in signals):
                continue
            if folder in verdicts or gate.excludes_rel(folder):
                continue
            gate.blocked[folder] = "; ".join(text for _, text in signals)
            gate.excluded.add(folder)
        if gate.blocked:
            shown = "; ".join(f'"{f}" ({why})' for f, why in gate.blocked_report()[:8])
            _log.warning(
                "context index: %d folder(s) look risky (many card stores, a series of dated "
                "copies, or a copy of the corpus itself) and could not be reviewed, so they are not indexed, not entered, and no "
                "store beneath them is imported or mounted: %s. To include one anyway, set "
                '"index": true for that folder in %s.',
                len(gate.blocked), shown, Path(cards_dir) / _FOLDER_REVIEW_FILE)
    except Exception:  # noqa: BLE001
        _log.info("context index: risk gate failed; falling back to the plain review", exc_info=True)
    return gate
