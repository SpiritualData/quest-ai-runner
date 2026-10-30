"""Card ceilings: one card per folder, file-set collapse, thin-card checks.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .context_bootstrap_support import _card_file_paths, _jaccard, _log, _merge_card_group


# ---------------------------------------------------------------------------
# Card inflation: three deterministic gates in front of every write
# ---------------------------------------------------------------------------
# A corpus of roughly 80,000 indexable files should produce a few HUNDRED cards. Two real stores
# held 8,557 and 32,070. None of the three causes was exotic: duplicate cards describing the same
# files under different names, one card per generated data file, and no ceiling of any kind. The
# gates below are all deterministic (no model call, no keyword gate) because a card that should
# never exist is not worth a model call to confirm.

# Two cards covering the same FILES are the same card, whatever they are called. Identical path
# sets collapse, and so does near-identical: at 0.8 the pair is describing one thing with one
# file's difference, which is how an incremental pass re-describes a folder it already carded.
_FILE_SET_DEDUP_JACCARD = 0.8


# A file-derived card EARNS its place by relating files to each other. One file is not a topic, it
# is a file, and the retrieval arms already find a file by its own path and terms.
_MIN_FILES_PER_CARD = 2


# DATA is never a single-file card: nothing a summary can say about a generated artefact is worth
# a card, and these are the files that change on every run, so each change used to spawn more
# cards. Markdown and code keep the single-file exception (a design note or one real module can
# genuinely be the distinct thing a card is about) provided no other card already covers it.
_DATA_FILE_EXTS = (".json", ".yaml", ".yml", ".jsonl", ".ndjson", ".csv", ".tsv", ".lock")


_DATA_FILE_NAMES = (
    "package.json", "package-lock.json", "composer.lock", "gemfile.lock", "cargo.lock",
    "poetry.lock", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "pipfile.lock", "go.sum",
)


_DATA_PATH_MARKERS = ("local_cache", "node_modules", ".cache", "__pycache__")


_STATE_FILE_RE = re.compile(r"(?:^|[_.-])state\.json$", re.IGNORECASE)


# The ceiling. ONE rule, and it is not a number somebody picked: a topic card describes a FOLDER,
# so a folder gets a card. The size of the index is then a fact about the corpus (how many folders
# of real content it has) rather than a quota invented in this file.
#
# An earlier version of this carried a global budget of one card per 25 indexable files with a
# floor of 50, plus five cards per "area". Both were made up. They also behaved badly in exactly
# the way an arbitrary limit does: the two stores this was written for came out of their cleanup
# holding more cards than the invented budget allowed, so the budget's only remaining effect was
# to refuse to index anything new, forever, and the question of what the number should be got
# handed to a human. A rule that has to be argued about by hand is the wrong rule. Measured after
# removing it: 198 and 84 folders of indexable content in the two corpora, so one card per folder
# is a few hundred cards, which is the number that was wanted all along.
#
# QAR_BOOTSTRAP_MAX_CARDS still exists for an operator who wants a hard cap on one pass, and it is
# opt-in with no default.
_MAX_CARDS_PER_FOLDER_DEFAULT = 8
# Why eight and not one: the indexing prompt itself asks for 2 to 8 topic cards per area of the corpus, so a rule of one
# per folder contradicted what the indexer is told to produce, and flagged a store as "inflated" (and refused to embed it)
# when it only held several topics about one folder's content. A real inflation (copies, loops) is hundreds of cards over
# the folder count, which eight per folder still catches.


def _positive_int_env(name: str, default: int) -> int:
    """A positive int from the environment, or ``default``. ``0`` means "no cap". Never raises."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning("context index: %s=%r is not an integer — using %d", name, raw, default)
        return default
    return value if value >= 0 else default


def max_cards_per_folder() -> int:
    return _positive_int_env("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", _MAX_CARDS_PER_FOLDER_DEFAULT)


def folders_for_room(uncovered: List[str], room: int, walk_root: Any) -> Tuple[List[str], List[str]]:
    """Of the files no card covers, the ones worth sending to the model when only ``room`` cards still fit.

    ``(sent, waiting)``. A card needs a folder, and a store with room for ``room`` more cards cannot hold
    cards for more than ``room`` folders, so the model is asked about that many folders and no more. The
    folders are chosen by how recently one of their files changed (newest first; ties by path), those with at
    least ``_MIN_FILES_PER_CARD`` uncovered files before those with fewer, since a thinner folder cannot
    be a card by itself. No folder is judged by its name: the folders the review excludes (an archive, a
    backup, generated output) are not in ``uncovered`` at all. What is not sent is returned as ``waiting``:
    those files are neither analysed nor "seen", so raising the limit (or a card moving out) picks them up
    on the next pass, and no model call is spent on a card the store would refuse.
    """
    by_folder: Dict[str, List[str]] = {}
    for rel in uncovered:
        by_folder.setdefault(rel.rsplit("/", 1)[0] if "/" in rel else ".", []).append(rel)
    if room <= 0 or len(by_folder) <= room:
        return list(uncovered), []

    def newest(folder: str) -> float:
        best = 0.0
        for rel in by_folder[folder]:
            try:
                best = max(best, os.stat(os.path.join(str(walk_root), rel)).st_mtime)
            except OSError:
                continue
        return best

    ranked = sorted(by_folder, key=lambda f: (0 if len(by_folder[f]) >= _MIN_FILES_PER_CARD else 1, -newest(f), f))
    chosen = set(ranked[:room])
    sent = [rel for rel in uncovered if (rel.rsplit("/", 1)[0] if "/" in rel else ".") in chosen]
    waiting = [rel for rel in uncovered if (rel.rsplit("/", 1)[0] if "/" in rel else ".") not in chosen]
    return sent, waiting


def path_is_data_file(rel: str) -> bool:
    """Whether ``rel`` is generated/derived data rather than something a card can be about."""
    text = str(rel or "").replace("\\", "/")
    name = text.rsplit("/", 1)[-1].lower()
    if name in _DATA_FILE_NAMES or _STATE_FILE_RE.search(name):
        return True
    if any(marker in text.lower() for marker in _DATA_PATH_MARKERS):
        return True
    return any(name.endswith(ext) for ext in _DATA_FILE_EXTS)


def card_folder_key(card: Dict[str, Any]) -> str:
    """The folder a card is about: the deepest directory common to all its files."""
    paths = [p.replace("\\", "/") for p in _card_file_paths(card)]
    if not paths:
        return ""
    dirs = [p.rsplit("/", 1)[0] if "/" in p else "." for p in paths]
    parts = dirs[0].split("/")
    for d in dirs[1:]:
        other = d.split("/")
        keep = 0
        while keep < min(len(parts), len(other)) and parts[keep] == other[keep]:
            keep += 1
        parts = parts[:keep]
        if not parts:
            break
    return "/".join(parts) if parts else "."


def card_preference_key(card: Dict[str, Any], index: int) -> Tuple[int, str, int]:
    """Sort key picking the BEST card of a duplicate group: most used, else earliest, else first.

    "Most used" is the honest signal that a card earns its keep -- something actually retrieved it
    and the work went well enough to leave the count behind. Creation time breaks the tie so the
    card other state already points at survives, rather than its newest restatement.
    """
    usage = int(card.get("usage_count") or 0)
    provenance = card.get("provenance")
    created = ""
    if isinstance(provenance, dict):
        created = str(provenance.get("created_at") or "")
    # An unknown creation time must not count as "earliest": sort it after every known one.
    return (-usage, created or "~", index)


def collapse_cards_by_file_set(
    new_cards: List[Dict[str, Any]],
    existing_cards: Optional[List[Dict[str, Any]]] = None,
    *,
    threshold: float = _FILE_SET_DEDUP_JACCARD,
) -> List[Dict[str, Any]]:
    """Collapse cards covering the same FILES into one. No model call, no keyword gate.

    This runs BEFORE the keyword clustering and the LLM merge decision, because the file set is
    the one thing about a card that is not a matter of judgement: two cards pinning the same paths
    are the same card however differently the model named or described them, and the keyword gate
    (two shared keywords) let plenty of those pairs through. Existing cards take part, so a new
    card that restates one already in the store is folded INTO it under its own id rather than
    written as a second copy.

    Returns the cards to write: every group that gained a new card (represented by its best
    member, files and keywords unioned) plus the new cards that duplicated nothing.
    """
    existing_cards = existing_cards or []
    passthrough: List[Dict[str, Any]] = []
    entries: List[Tuple[int, Dict[str, Any], bool, Set[str]]] = []
    index = 0
    for card in existing_cards:
        files = set(_card_file_paths(card))
        if files:
            entries.append((index, card, True, files))
        index += 1
    for card in new_cards:
        files = set(_card_file_paths(card))
        if not files:
            # Not file-derived (a conversation or run card). Nothing here can judge it.
            passthrough.append(card)
        else:
            entries.append((index, card, False, files))
        index += 1

    # Inverted path -> group index, so each card is compared only against groups it could
    # possibly match. An overlap at or above any positive threshold requires at least one shared
    # path, so this is exact, not a heuristic -- and it is the difference between a linear pass and
    # comparing 32,000 existing cards against every group on every bootstrap.
    groups: List[Dict[str, Any]] = []
    group_of_path: Dict[str, Set[int]] = {}
    for item in entries:
        _, _, _, files = item
        candidate_indexes: Set[int] = set()
        for path in files:
            candidate_indexes |= group_of_path.get(path, set())
        target: Optional[int] = None
        for gi in sorted(candidate_indexes):
            group = groups[gi]
            if files == group["files"] or _jaccard(files, group["files"]) >= threshold:
                target = gi
                break
        if target is None:
            target = len(groups)
            groups.append({"members": [], "files": set()})
        groups[target]["members"].append(item)
        groups[target]["files"] |= files
        for path in groups[target]["files"]:
            group_of_path.setdefault(path, set()).add(target)

    out: List[Dict[str, Any]] = []
    collapsed = 0
    for group in groups:
        members = group["members"]
        has_new = any(not is_existing for _, _, is_existing, _ in members)
        if not has_new:
            continue                       # existing cards only: nothing new to write
        if len(members) == 1:
            out.append({**members[0][1]})
            continue
        ordered = sorted(members, key=lambda m: card_preference_key(m[1], m[0]))
        keeper = ordered[0][1]
        rest = [m[1] for m in ordered[1:]]
        out.append(_merge_card_group([keeper] + rest))
        collapsed += len(rest)
    if collapsed:
        _log.info("context index: %d card(s) collapsed into an existing card covering the same "
                  "files (identical path set or >= %.0f%% overlap)", collapsed, threshold * 100)
    return passthrough + out


def card_is_too_thin(card: Dict[str, Any], covered_elsewhere: Set[str]) -> str:
    """Why this file-derived card does not earn a card, or "" when it does.

    A card with no files at all is never judged here (that is a conversation or run card).
    """
    paths = _card_file_paths(card)
    if not paths or len(paths) >= _MIN_FILES_PER_CARD:
        return ""
    only = paths[0]
    if path_is_data_file(only):
        return "single generated/data file"
    if only in covered_elsewhere:
        return "single file already covered by another card"
    return ""


def apply_card_ceilings(
    new_cards: List[Dict[str, Any]],
    existing_cards: List[Dict[str, Any]],
    *,
    max_cards: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drop NEW cards beyond one per folder, and beyond ``max_cards`` when an operator set one.

    The per-folder rule is the whole ceiling, and it is structural rather than numeric: a topic
    card describes a folder, so the number of cards a corpus can produce is the number of folders
    of real content it has. Nothing here invents a quota.

    A card whose id is already in the store is always kept: it is a refresh of a card that exists,
    so dropping it would only leave the store stale, never smaller. Existing cards DO count toward
    the per-folder rule, because it is about the state of the store rather than one pass, but only
    FILE-DERIVED ones do: a conversation or run card has no files, occupies no folder, and arrives
    from being used rather than from the corpus.

    Truncation is always logged, at DEBUG because it is the expected outcome, with the numbers.
    A ceiling nobody can see is indistinguishable from a bug, which this file has already learned once (see
    ``QAR_BOOTSTRAP_MAX_FILES``).
    """
    per_folder = max_cards_per_folder()

    existing_by_id = {c.get("id"): c for c in existing_cards if c.get("id")}
    incoming_ids = {c.get("id") for c in new_cards if c.get("id")}

    folder_counts: Dict[str, int] = {}
    total = 0
    for cid, card in existing_by_id.items():
        folder = card_folder_key(card)
        if cid in incoming_ids or not folder:
            continue                        # counted when its refresh is admitted, or not a folder
        total += 1
        folder_counts[folder] = folder_counts.get(folder, 0) + 1

    kept: List[Dict[str, Any]] = []
    dropped = {"folder": 0, "max_cards": 0}
    for card in new_cards:
        folder = card_folder_key(card)
        is_refresh = card.get("id") in existing_by_id
        if not is_refresh and folder:
            if max_cards is not None and total >= max_cards:
                dropped["max_cards"] += 1
                continue
            if per_folder and folder_counts.get(folder, 0) >= per_folder:
                dropped["folder"] += 1
                continue
        kept.append(card)
        if folder:
            total += 1
            folder_counts[folder] = folder_counts.get(folder, 0) + 1

    if any(dropped.values()):
        # Expected behavior (one card per folder, the operator's max_cards), so DEBUG, not WARNING
        # (fca3b8d silenced the noise); still logged with the numbers so it is never invisible.
        parts = []
        if dropped["folder"]:
            parts.append(f"{dropped['folder']} (folder already has a card)")
        if dropped["max_cards"]:
            parts.append(f"{dropped['max_cards']} (over max_cards)")
        _log.debug(
            "context index: skipped %d card(s) during ceiling application (normal): %s. "
            "Each folder's topics are capped at QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER; raise it if needed.",
            sum(dropped.values()), "; ".join(parts),
        )
    return kept
