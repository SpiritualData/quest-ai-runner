"""What a tidy of a card store moves, and why: one pure function over card dicts, for every repository.

The rules were written and reviewed as ``scripts/quarantine_cards.py`` (an operator's tool over a folder of card
files). They live here now, as a function of ``{key: card}`` alone, so the same plan is made for a local folder
store, the Quest backend and a Qdrant collection (``card_store_tidy`` applies it through the ``CardRepository``
port), and the script imports it instead of keeping a second copy.

  item-id     a card stored under a vector point id (``card:<id>``); its notes join the card it names.
  compounded  an id that stacks import prefixes, moved only when a SURVIVING card (not itself moved, not a
              compounded candidate) pins the same or more files; with none it is the only copy and is re-keyed to its one-hop id
              (``rekey_target``) so the guard stops refusing it.
  excluded    more than half of its files sit under folders the folder review excludes for good.
  surplus     the same files as another card (identical, or an overlap at or above the threshold); the best
              keeper stays (``keeper_rank``: most used, an index-pass card over an annotated one, best name
              match, earliest, id). With ``distinct_topics`` (what runs without a person) the same files under
              another name are two topics of one file, and both stay.
  thin        one file, and that file is generated data.
  covered     (opt-in) the files are held by a card of ANOTHER store named by the caller.

Never moved: a card ``card_origin.protection`` names (managed, used, notes or edits, quest tags, conventions,
learned from a run, written by a person, edited since the index wrote it), except a provable compounded copy
whose survivor can take everything it holds without loss (``fold_card``), and a stray ``card:<id>`` whose notes
join the card it names. Pure: reads only its arguments (``excluded_prefixes_from_review`` reads the folder
review cache, a sidecar of the lane's local card folder, and nothing else).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import card_origin
from .card_store_hygiene import (card_paths, fold_card, jaccard, keeper_rank, name_key_of,
                                 protection_reason as is_protected)

ALL_REASONS = ("item-id", "compounded", "excluded", "surplus", "thin", "covered")
DEFAULT_REASONS = ("item-id", "compounded", "excluded", "surplus", "thin")
# What the id alone proves to be a copy: the automatic tidy of a store that is under its limit applies only these.
PROVABLE_REASONS = ("item-id", "compounded")


def excluded_prefixes_from_review(cards_dir: Path) -> List[str]:
    """Folders the cached folder review excludes, read with the CURRENT rules.

    The cache file is data, not a decision: it holds a verdict per folder, and which of those verdicts actually
    excludes a folder is decided here by the same rule the bootstrap uses. A final verdict (a nested repository,
    a duplicate tree) stands whatever its children say; only a model skip that the model itself called "mixed"
    is superseded by its kept children.
    """
    from .context_folder_review import _has_kept_child, verdict_is_final
    try:
        loaded = json.loads((Path(cards_dir) / "folder_review.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    verdicts = loaded.get("folders") if isinstance(loaded, dict) else None
    if not isinstance(verdicts, dict):
        return []
    expanded = {"/".join(f.split("/")[:-1]) for f in verdicts if "/" in f}
    excluded: List[str] = []
    for folder, verdict in verdicts.items():
        if not isinstance(verdict, dict) or verdict.get("index", True):
            continue
        superseded = (not verdict_is_final(verdict) and bool(verdict.get("mixed"))
                      and folder in expanded and _has_kept_child(verdicts, folder))
        if not superseded:
            excluded.append(folder)
    return sorted(excluded)


def under_any_prefix(rel: str, prefixes: List[str]) -> Optional[str]:
    """The first prefix ``rel`` sits under, or None."""
    for prefix in prefixes:
        if not prefix or prefix == ".":
            continue
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return prefix
    return None


def strip_prefix(rel: str, prefixes: List[str]) -> str:
    """``rel`` without the first leading prefix folder it sits under."""
    for prefix in prefixes:
        head = prefix.strip("/")
        if head and rel.startswith(head + "/"):
            return rel[len(head) + 1:]
    return rel


def stray_plan(key: str, stray: Dict[str, Any], cards: Dict[str, Dict[str, Any]], own_store_id: str,
               quarantined: Callable[[str], bool]) -> Tuple[str, str, str]:
    """``(target key, detail, "")`` for a card stored under a vector point id, or ``("", "", why)`` when it stays."""
    if stray.get("managed_by"):
        return "", "", "managed by another system"
    target_id = card_origin.card_id_of_item(key, own_store_id)
    if card_origin.is_store_qualified(target_id):
        return "", "", "names a card mounted from another store"
    if card_origin.card_id_problem(target_id):
        return "", "", "the card it stands for could not be stored under that id"
    target = cards.get(target_id)
    if target is not None:
        merged, why = fold_card(stray, target)
        if merged is None:
            return "", "", why
        return target_id, f"stored under a vector point id; its notes join {target_id}", ""
    if quarantined(target_id):
        return "", "", "the card it stands for was moved out of the index"
    return target_id, f"stored under a vector point id; it becomes {target_id}", ""


def rekey_target(card_id: str) -> str:
    """The one-hop id a compounded copy that is its store's only copy is re-keyed to, or "" when none is clear.

    ``<first namespace>--<topic>``: the shape ``card_origin.import_id`` gives a one-level import, with the
    stacked middle hops dropped. A function of the id alone, so every run picks the same id.
    """
    parts = card_origin.namespace_segments(card_id)
    if len(parts) < 3 or len(card_id) >= card_origin.MAX_ID_CHARS - 10:
        return ""            # a truncated id has lost the end of its topic: no clean name can be read from it
    return f"{parts[0]}{card_origin.NAMESPACE_SEP}{parts[-1]}"


def plan_cards(
    cards: Dict[str, Dict[str, Any]],
    *,
    reasons: Set[str],
    excluded: List[str],
    threshold: float,
    covered_by: Optional[List[Dict[str, Dict[str, Any]]]] = None,
    strip_prefixes: Optional[List[str]] = None,
    own_store_id: str = "",
    quarantined: Callable[[str], bool] = lambda key: False,
    distinct_topics: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Decide what moves. ``cards`` is ``{key: card}`` in the order to consider them (the key is what the
    repository stores the card under). Returns ``(entries, skipped_counts)``; touches nothing.

    Each entry: ``reason``, ``detail``, ``key``, ``card_id``, ``files`` (count), ``usage_count``, ``created_at``,
    and, when another card stands for it, ``keeper`` (that card's key). A protected compounded copy whose
    holdings must join its survivor first carries ``joins`` (the survivor's key); an item-id stray whose card
    does not exist yet carries ``becomes`` (the key it is written under).

    ``distinct_topics`` keeps a card that pins the same files as its keeper under ANOTHER name (two topics of one
    file, a review finding): the automatic tidy and the repository tidy pass it, because nobody reads their plan
    before it is applied. The operator's script leaves it off by default, and its reader judges.
    """
    items = list(cards.items())
    entries: List[Dict[str, Any]] = []
    decided: Set[str] = set()
    moved_reason: Dict[str, str] = {}
    keeper_of: Dict[str, str] = {}
    skipped: Dict[str, int] = {}

    def record(key: str, card: Dict[str, Any], reason: str, detail: str, **extra: Any) -> None:
        prov = card.get("provenance")
        entries.append({
            "reason": reason, "detail": detail, "key": key, "card_id": str(card.get("id") or key),
            "files": len(card_paths(card)), "usage_count": card.get("usage_count", 0),
            "created_at": str(prov.get("created_at") or "") if isinstance(prov, dict) else "",
            **extra,
        })
        decided.add(key)
        moved_reason[key] = reason

    def note_skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    # 0. Cards stored under a vector point id: their notes join the card they name when the move is applied.
    if "item-id" in reasons:
        for key, card in items:
            if str(card.get("id") or key).startswith(card_origin.ITEM_PREFIX):
                target, detail, why = stray_plan(key, card, cards, own_store_id, quarantined)
                if why:
                    note_skip(f"vector-point-id card kept: {why}")
                    continue
                extra = {"keeper": target} if target in cards else {"becomes": target}
                record(key, card, "item-id", detail, **extra)

    # 1. Compounded candidates: provable from the id alone, but MOVED only in step 6, once the survivors are known.
    pending: Dict[str, str] = {}
    if "compounded" in reasons:
        for key, card in items:
            evidence = card_origin.compounding_evidence(str(card.get("id") or key))
            if not evidence:
                continue
            if card.get("managed_by"):
                note_skip("compounded but managed by another system")
                continue
            pending[key] = evidence

    # 2. Cards mostly inside a folder the review excludes for good.
    if "excluded" in reasons and excluded:
        for key, card in items:
            if key in decided:
                continue
            paths = card_paths(card)
            if not paths:
                continue
            inside = [h for h in (under_any_prefix(p, excluded) for p in paths) if h]
            if len(inside) * 2 <= len(paths):
                continue
            protection = is_protected(card)
            if protection:
                note_skip(f"excluded-folder card kept: {protection}")
                continue
            record(key, card, "excluded",
                   f"{len(inside)} of {len(paths)} file(s) under excluded folders, e.g. {inside[0]}")

    # 3. Surplus cards covering the same file set as a card that is kept.
    if "surplus" in reasons:
        groups: List[Dict[str, Any]] = []
        candidates = [(i, key, card, set(card_paths(card))) for i, (key, card) in enumerate(items)
                      if key not in decided and key not in pending and card_paths(card)]
        # An overlap at or above any positive threshold needs a shared file, so comparing only the groups that
        # share a path is exact and keeps a large store from becoming a quadratic scan.
        group_of_path: Dict[str, Set[int]] = {}
        for index, key, card, files in candidates:
            candidate_indexes: Set[int] = set()
            for rel in files:
                candidate_indexes |= group_of_path.get(rel, set())
            target: Optional[int] = None
            for gi in sorted(candidate_indexes):
                group = groups[gi]
                if files == group["files"] or jaccard(files, group["files"]) >= threshold:
                    target = gi
                    break
            if target is None:
                target = len(groups)
                groups.append({"members": [], "files": set()})
            groups[target]["members"].append((index, key, card))
            groups[target]["files"] |= files
            for rel in groups[target]["files"]:
                group_of_path.setdefault(rel, set()).add(target)
        for group in groups:
            members = group["members"]
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda m: keeper_rank(m[2], m[1]))
            keeper_key = ordered[0][1]
            keeper_id = str(ordered[0][2].get("id") or keeper_key)
            for _, key, card in ordered[1:]:
                protection = is_protected(card)
                if protection:
                    note_skip(f"duplicate card kept: {protection}")
                    continue
                if distinct_topics and name_key_of(card) != name_key_of(ordered[0][2]):
                    # Two topics about one file are two cards (a handbook on billing and on sign-in): the same
                    # files under another name is not a copy (the review finding the store's own tidy also keeps).
                    note_skip("duplicate card kept: same files, but it is about something else (its name differs)")
                    continue
                record(key, card, "surplus", f"same files as {keeper_id}", keeper=keeper_key)
                keeper_of[key] = keeper_key

    # 4. Single generated-data-file cards.
    if "thin" in reasons:
        from .context_card_ceilings import card_is_too_thin, path_is_data_file
        covered_by_kept: Set[str] = set()
        for key, card in items:
            if key in decided or key in pending:
                continue
            paths = card_paths(card)
            if len(paths) >= 2:
                covered_by_kept.update(paths)
        for key, card in items:
            if key in decided:
                continue
            paths = card_paths(card)
            if len(paths) != 1 or not path_is_data_file(paths[0]):
                continue
            protection = is_protected(card)
            if protection:
                note_skip(f"single data-file card kept: {protection}")
                continue
            record(key, card, "thin", card_is_too_thin(card, covered_by_kept) or "single generated/data file")

    # 5. Cards whose files another store already holds (identical or a superset).
    if "covered" in reasons and covered_by:
        strips = list(strip_prefixes or [])

        def normalized(card: Dict[str, Any]) -> Set[str]:
            return {strip_prefix(p, strips) for p in card_paths(card)}

        for number, other_cards in enumerate(covered_by, start=1):
            holders: Dict[str, Set[int]] = {}
            other_sets: List[Set[str]] = []
            other_ids: List[str] = []
            for other_key, other_card in other_cards.items():
                files = normalized(other_card)
                other_sets.append(files)
                other_ids.append(str(other_card.get("id") or other_key))
                for rel in files:
                    holders.setdefault(rel, set()).add(len(other_sets) - 1)
            for key, card in items:
                if key in decided or key in pending:
                    continue
                files = normalized(card)
                if not files:
                    continue
                matches: Optional[Set[int]] = None
                for rel in files:
                    matches = set(holders.get(rel, set())) if matches is None else matches & holders.get(rel, set())
                    if not matches:
                        break
                if not matches:
                    continue
                protection = is_protected(card)
                if protection:
                    note_skip(f"covered card kept: {protection}")
                    continue
                index = min(matches)
                kind = "same files as" if other_sets[index] == files else "files inside those of"
                record(key, card, "covered", f"{kind} {other_ids[index]} in covered-by store {number}")

    # 6. Compounded candidates not taken above: moved only when a surviving equivalent exists, i.e. another card
    #    neither moved in this run nor a candidate, with the same or more files.
    if pending:
        claimed_rekeys: Set[str] = set()
        holders_by_path: Dict[str, Set[int]] = {}
        survivors: List[Tuple[str, str]] = []          # (card id, key)
        for key, card in items:
            files = set(card_paths(card))
            if key in decided or key in pending or not files:
                continue
            survivors.append((str(card.get("id") or key), key))
            for rel in files:
                holders_by_path.setdefault(rel, set()).add(len(survivors) - 1)
        for key, card in items:
            if key in decided or key not in pending:
                continue
            files = set(card_paths(card))
            matches = None
            for rel in files:
                matches = set(holders_by_path.get(rel, set())) if matches is None \
                    else matches & holders_by_path.get(rel, set())
                if not matches:
                    break
            if files and not matches:
                clean = rekey_target(str(card.get("id") or key))
                if (not clean or clean in cards or clean in claimed_rekeys or clean in pending
                        or card_origin.card_id_problem(clean) or quarantined(clean)):
                    note_skip("compounded card kept: its only copy in this store")
                    continue
                claimed_rekeys.add(clean)
                record(key, card, "compounded",
                       f"{pending[key]}; the only copy, so it is re-keyed to {clean} (the card moves whole)",
                       becomes=clean)
                continue
            detail = pending[key]
            extra: Dict[str, Any] = {}
            if matches:
                survivor_id, survivor_key = survivors[min(matches)]
                detail += f"; equivalent kept: {survivor_id}"
                extra["keeper"] = survivor_key
                protection = is_protected(card)
                if protection:
                    survivor = cards.get(survivor_key)
                    merged, why = fold_card(card, survivor) if survivor is not None else (None, "it is gone")
                    if merged is None:
                        note_skip(f"compounded card kept: {protection}, and it cannot join {survivor_id}: {why}")
                        continue
                    detail += f"; what it holds ({protection}) joins that card first"
                    extra["joins"] = survivor_key
            elif is_protected(card):
                note_skip(f"compounded card kept: {is_protected(card)}")
                continue
            record(key, card, "compounded", detail, **extra)

    # A surplus keeper can itself be moved by a later reason: say so on the surplus entries.
    for entry_key, keeper_key in keeper_of.items():
        if keeper_key in moved_reason:
            for entry in entries:
                if entry["key"] == entry_key and entry["reason"] == "surplus":
                    entry["detail"] += f" ({keeper_key} is also moved: {moved_reason[keeper_key]})"
    return entries, skipped
