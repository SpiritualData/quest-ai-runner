"""One tidy for every card repository: plan, export, apply and undo through the ``CardRepository`` port.

A local folder store, the Quest backend and a Qdrant collection hold the same cards and inflate the same ways
(copy-import ids, cards about excluded nested repositories, duplicates of one file set). The plan is the pure
``card_store_tidy_plan.plan_cards`` over the cards the repository lists; this module carries it out with nothing
but the port (``load_all``, ``read``, ``write``, ``delete``, and ``quarantine`` where the repository has one):

  1. EXPORT FIRST. Before the first write, every card the tidy will remove and every keeper it will update is
     written, as it is now, to ``<cards_dir>/tidy-state/<run>-tidy-export.json`` (mode 600, folder 700; the
     store's identity inside, never a key), next to a manifest the run appends one line to per change. A backend
     store has no quarantine folder, so this export is what makes the tidy reversible there.
  2. KEEPER FIRST, THEN REMOVAL. For each card that stands for others, its current revision is read again and
     compared with the plan's; a changed card is skipped (other writers exist). What a removed card holds joins
     the keeper (``fold_card`` for a stray ``card:<id>`` or a protected compounded copy; for every removed card its
     keywords, as a union in the keeper's order first) and the keeper is written once; then each removed card is
     read again and removed: MOVED into ``_quarantine/<reason>/`` where the repository can (a local folder), else
     deleted. A keeper's name, summary, files and usage are never changed, except by ``fold_card`` for a provable
     copy, which keeps what the copy held.
  3. Batches with a pause between them; the first failed write or removal stops the run (the export still undoes
     what was done); a final pass lists the store again and checks every removal and keeper.

``RepositoryTidy.undo(manifest)`` re-creates every removed card and restores every keeper it updated, through the
same port, newest change first; a keeper changed since the tidy is left alone and counted. A second run of the
same plan finds nothing (it is idempotent: what it removed is gone, what it joined is already on the keeper).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from esoi import implementation

from . import card_origin
from .card_repository import FilesystemCardRepository
from .card_store_hygiene import fold_card, has_cheap_evidence, no_pace, protection_reason as is_protected
from .card_store_tidy_plan import DEFAULT_REASONS, PROVABLE_REASONS, excluded_prefixes_from_review, plan_cards

log = logging.getLogger("quest-ai-runner.card-store")

STATE_DIR_NAME = "tidy-state"
EXPORT_SUFFIX = "-tidy-export.json"
MANIFEST_SUFFIX = "-store-tidy-manifest.jsonl"
MANIFEST_KIND = "store-tidy"
PLAN_FILE = "last-plan.json"
BATCH_SIZE = 25
# Where a tidy runs on its own: "apply" carries the plan out, "plan" writes it and says how to apply it, "off" does
# neither. Unset: "apply" for a local folder store (a move into _quarantine, undone by moving it back) and "plan"
# for every other repository, whose cards are shared team data: deleting them is the owner's call.
MODES = ("apply", "plan", "off")
# Fields only a backend sets; two reads of an unchanged card may differ in them, an undo never writes them.
SERVER_FIELDS = ("updated_at", "revision", "owner_user_id", "user_id", "card_id")


def is_local_folder(repo: Any) -> bool:
    return isinstance(repo, FilesystemCardRepository)


def resolve_mode(setting: Optional[str], repo: Any) -> str:
    """The tidy mode for ``repo`` from the lane's setting (``RunnerConfig.card_tidy``); see ``MODES``."""
    value = str(setting or "").strip().lower()
    if value in MODES:
        return value
    if value and value != "auto":
        log.warning("card_tidy=%r is not one of %s; using the default for this store", setting, ", ".join(MODES))
    return "apply" if is_local_folder(repo) else "plan"


def state_dir_for(cards_dir: Any) -> Path:
    return Path(cards_dir) / STATE_DIR_NAME


def store_identity(repo: Any, cards_dir: Any) -> Dict[str, str]:
    """What store a tidy ran on, without anything secret: the repository kind, its address and scope, and the card
    folder. An undo refuses a manifest whose identity is not this one's."""
    out = {"repository": type(repo).__name__, "cards_dir": str(Path(cards_dir).resolve())}
    for name in ("base_url", "team_id", "org_id", "user_id", "store_id", "collection_name", "url"):
        value = getattr(repo, name, None)
        if isinstance(value, str) and value:
            out[name] = value
    return out


def card_revision(card: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """A card's revision stamp: the backend's own ``revision`` and ``updated_at`` when it keeps them, and a hash of
    everything else. ``{}`` for no card. Compare two with ``same_revision``."""
    if not isinstance(card, dict):
        return {}
    stamp = {"sha": hashlib.sha256(json.dumps(without_server_fields(card), sort_keys=True, default=str)
                                   .encode("utf-8")).hexdigest()}
    for name in ("revision", "updated_at"):
        if card.get(name) not in (None, ""):
            stamp[name] = str(card[name])
    return stamp


def same_revision(a: Dict[str, str], b: Dict[str, str]) -> bool:
    """Whether two stamps are of one unchanged card: by the backend's revision when both carry one, else by
    ``updated_at`` when both carry it, else by the hash of the card."""
    if not a or not b:
        return False
    for name in ("revision", "updated_at"):
        if name in a and name in b:
            return a[name] == b[name]
    return a["sha"] == b["sha"]


def without_server_fields(card: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in card.items() if k not in SERVER_FIELDS}


def union_keywords(keeper: Dict[str, Any], removed: Dict[str, Any]) -> bool:
    """Add ``removed``'s keywords that ``keeper`` lacks, after its own, in place. True when any was added."""
    def words(card: Dict[str, Any]) -> List[str]:
        raw = card.get("keywords") or []
        return [raw] if isinstance(raw, str) else [str(k) for k in raw if str(k).strip()]
    held = words(keeper)
    seen = {k.lower() for k in held}
    added = [k for k in words(removed) if k.lower() not in seen and not seen.add(k.lower())]
    if added:
        keeper["keywords"] = held + added
    return bool(added)


# --------------------------------------------------------------------------------------------
# The plan for a repository, and when one is due
# --------------------------------------------------------------------------------------------

@dataclass
class StorePlan:
    entries: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    scanned: int = 0
    reasons: Tuple[str, ...] = ()
    why: str = ""                     # what made it due (the automatic trigger), "" by hand

    def by_reason(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for entry in self.entries:
            out[entry["reason"]] = out.get(entry["reason"], 0) + 1
        return out

    def fingerprint(self) -> str:
        keys = sorted(f"{e['reason']}:{e['key']}" for e in self.entries)
        return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]

    def summary(self) -> str:
        return ", ".join(f"{name} {n:,}" for name, n in sorted(self.by_reason().items()))


def plan_store(cards: Dict[str, Dict[str, Any]], *, cards_dir: Any, reasons=DEFAULT_REASONS,
               own_store_id: str = "", repo: Any = None, threshold: Optional[float] = None,
               distinct_topics: bool = True) -> StorePlan:
    """The tidy plan for the cards a repository holds (``{key: card}``), with the folder review's exclusions read
    from the lane's local card folder. Pure apart from reading that review cache. ``distinct_topics=False`` (the
    operator's ``--merge-topics``) joins a card that pins a keeper's files under another name onto that keeper."""
    from .context_card_ceilings import _FILE_SET_DEDUP_JACCARD
    finder = getattr(repo, "quarantined", None)
    ordered = {key: cards[key] for key in sorted(cards)}
    entries, skipped = plan_cards(
        ordered, reasons=set(reasons), excluded=excluded_prefixes_from_review(Path(cards_dir)),
        threshold=_FILE_SET_DEDUP_JACCARD if threshold is None else threshold, own_store_id=own_store_id,
        quarantined=(lambda key: finder(key) is not None) if callable(finder) else (lambda key: False),
        distinct_topics=distinct_topics)
    return StorePlan(entries=entries, skipped=skipped, scanned=len(cards), reasons=tuple(reasons))


def due(cards: Dict[str, Dict[str, Any]], limit: int) -> Tuple[Tuple[str, ...], str]:
    """``(reasons, why)`` when a store needs the automatic tidy, else ``((), "")``. Reads the ids and the count only.

    Over its card limit, every default reason (the limit itself never removes a card; the tidy does). Under it, only
    what an id proves to be a copy (a vector point id, or an id that stacks import prefixes)."""
    if limit > 0 and len(cards) > limit:
        return DEFAULT_REASONS, f"it holds {len(cards):,} cards, over its limit of {limit:,}"
    if any(has_cheap_evidence(str(key)) for key in cards):
        return PROVABLE_REASONS, "it holds cards whose ids prove them copies"
    return (), ""


# --------------------------------------------------------------------------------------------
# Doing it, and taking it back
# --------------------------------------------------------------------------------------------

@dataclass
class TidyRun:
    removed: int = 0
    updated: int = 0
    created: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    failed: str = ""                  # why the run stopped early, "" when it ran to the end
    export: Optional[Path] = None
    manifest: Optional[Path] = None
    removed_keys: List[str] = field(default_factory=list)
    verified: bool = False
    problems: List[str] = field(default_factory=list)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


@dataclass
class UndoRun:
    restored: int = 0
    reverted: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    failed: str = ""

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by its owner only (mode 600), atomically."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass
    return path


@implementation("bound-card-growth", "repository_tidy")
class RepositoryTidy:
    """Carry out a ``StorePlan`` on any ``CardRepository``, reversibly (see the module docstring).

    ``cards_dir`` is the lane's local card folder (for a backend store: where its listing cache and folder review
    live); the export and the manifest go to ``<cards_dir>/tidy-state/``. ``pace`` is called between groups,
    ``batch_pause`` seconds are slept after every ``batch_size`` changes, ``should_stop`` ends the run between
    groups. Never raises; a failure is in ``TidyRun.failed``.
    """

    def __init__(self, repo: Any, cards_dir: Any, *, state_dir: Optional[Path] = None,
                 pace: Callable[[], None] = no_pace, should_stop: Callable[[], bool] = lambda: False,
                 batch_size: int = BATCH_SIZE, batch_pause: float = 0.0) -> None:
        self.repo = repo
        self.cards_dir = Path(cards_dir)
        self.state_dir = Path(state_dir) if state_dir is not None else state_dir_for(cards_dir)
        self.pace = pace
        self.should_stop = should_stop
        self.batch_size = max(1, int(batch_size))
        self.batch_pause = max(0.0, float(batch_pause))
        self.changes = 0

    # -- locks ----------------------------------------------------------------------------------

    def folder_lock(self):
        """The local folder's write lock (shared with every process writing it), or nothing for a backend."""
        if is_local_folder(self.repo):
            from .card_store_lock import StoreLock
            return StoreLock.for_dir(str(self.cards_dir))
        return contextlib.nullcontext()

    def process_lock(self):
        """Held for a whole tidy or undo, so two processes never tidy one store together."""
        from .card_store_lock import TIDY_LOCK_FILE, StoreLock
        where = self.cards_dir if is_local_folder(self.repo) else private_dir(self.state_dir)
        return StoreLock.for_dir(str(where), TIDY_LOCK_FILE)

    def changed(self, lock: Any) -> None:
        self.changes += 1
        bump = getattr(lock, "bump", None)
        if callable(bump):
            bump()
        if self.changes % self.batch_size == 0 and self.batch_pause:
            time.sleep(self.batch_pause)

    # -- apply ----------------------------------------------------------------------------------

    def export(self, plan: StorePlan, cards: Dict[str, Dict[str, Any]], run: TidyRun) -> None:
        """Write the export and open the manifest BEFORE anything is changed. Raises OSError when it cannot."""
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{os.urandom(3).hex()}"
        folder = private_dir(self.state_dir)
        removed = {e["key"] for e in plan.entries}
        keepers = {e[k] for e in plan.entries for k in ("keeper", "joins") if e.get(k) and e[k] not in removed}
        identity = store_identity(self.repo, self.cards_dir)
        run.export = folder / f"{stamp}{EXPORT_SUFFIX}"
        write_private(run.export, json.dumps({
            "kind": MANIFEST_KIND, "store": identity, "at": card_origin.now_iso(), "plan": plan.entries,
            "cards": {k: cards[k] for k in sorted(removed) if k in cards},
            "keepers": {k: cards[k] for k in sorted(keepers) if k in cards},
        }, indent=1, default=str) + "\n")
        run.manifest = folder / f"{stamp}{MANIFEST_SUFFIX}"
        write_private(run.manifest, json.dumps({"kind": MANIFEST_KIND, "store": identity,
                                                "export": run.export.name, "at": card_origin.now_iso()}) + "\n")

    def journal(self, run: TidyRun, line: Dict[str, Any]) -> None:
        with open(run.manifest, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**line, "at": card_origin.now_iso()}) + "\n")
            fh.flush()

    def remove(self, key: str, reason: str) -> bool:
        move = getattr(self.repo, "quarantine", None)
        return bool(move(key, reason)) if callable(move) else bool(self.repo.delete(key))

    def apply(self, plan: StorePlan, cards: Dict[str, Dict[str, Any]]) -> TidyRun:
        """Carry out ``plan``, made from ``cards`` (the listing the plan read). Never raises."""
        run = TidyRun()
        if not plan.entries:
            run.verified = True
            return run
        lock = self.process_lock()
        if not lock.acquire(blocking=False):
            run.failed = "another process is tidying this store"
            return run
        try:
            try:
                self.export(plan, cards, run)
            except OSError as exc:
                run.failed = f"the export could not be written ({exc}); nothing was changed"
                return run
            removed = {e["key"] for e in plan.entries}
            groups: Dict[str, List[Dict[str, Any]]] = {}
            solo: List[Dict[str, Any]] = []
            for entry in plan.entries:
                keeper = entry.get("joins") or entry.get("keeper")
                if keeper and keeper not in removed:
                    groups.setdefault(keeper, []).append(entry)
                else:
                    solo.append(entry)
            work = [(k, members) for k, members in groups.items()] + [("", [e]) for e in solo]
            for keeper_key, members in work:
                if self.should_stop() or run.failed:
                    break
                with self.folder_lock() as held:
                    self.settle_group(keeper_key, members, cards, run, held)
                self.pace()
            self.verify(run, plan, cards)
        except Exception as exc:  # noqa: BLE001
            run.failed = run.failed or f"stopped on an error: {exc}"
            log.warning("card tidy: stopped on an error; the export %s undoes what was done", run.export,
                        exc_info=True)
        finally:
            if run.manifest is not None:
                try:
                    self.journal(run, {"summary": {"removed": run.removed, "updated": run.updated,
                                                   "created": run.created, "skipped": run.skipped,
                                                   "failed": run.failed, "verified": run.verified}})
                except OSError:
                    pass
            lock.release()
        return run

    def current(self, key: str, planned: Optional[Dict[str, Any]], run: TidyRun, what: str) -> Optional[Dict[str, Any]]:
        """``key``'s card as it is now when its revision is still the planned one, else None (counted)."""
        now = self.repo.read(key)
        if now is None:
            run.skip(f"{what} is gone")
            return None
        if not same_revision(card_revision(now), card_revision(planned)):
            run.skip(f"{what} changed since the plan")
            return None
        return now

    def settle_group(self, keeper_key: str, members: List[Dict[str, Any]], cards: Dict[str, Dict[str, Any]],
                     run: TidyRun, lock: Any) -> None:
        keeper: Optional[Dict[str, Any]] = None
        if keeper_key:
            keeper = self.repo.read(keeper_key)
            if keeper is None or not same_revision(card_revision(keeper), card_revision(cards.get(keeper_key))):
                for _ in members:
                    run.skip("the card that stands for it is gone or changed since the plan")
                return
        merged = json.loads(json.dumps(keeper)) if keeper is not None else None
        ready: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for entry in members:
            now = self.current(entry["key"], cards.get(entry["key"]), run, "the card")
            if now is None:
                continue
            joins = entry["reason"] == "item-id" or bool(entry.get("joins"))
            if is_protected(now) and not joins:
                run.skip(f"it gained something since the plan: {is_protected(now)}")
                continue
            if merged is not None:
                if joins:
                    folded, why = fold_card(now, merged)
                    if folded is None:
                        run.skip(why)
                        continue
                    merged = folded
                union_keywords(merged, now)
            ready.append((entry, now))
        if not ready:
            return
        if merged is not None and merged != keeper:
            if not self.repo.write(keeper_key, without_server_fields(merged)):
                run.failed = f"the card {keeper_key} could not be updated"
                return
            after = self.repo.read(keeper_key)
            self.journal(run, {"op": "update", "key": keeper_key, "before": card_revision(keeper),
                               "after": card_revision(after if after is not None else merged)})
            run.updated += 1
            self.changed(lock)
        for entry, now in ready:
            becomes = entry.get("becomes")
            if becomes:
                if self.repo.read(becomes) is not None:
                    run.skip("the card it would become exists now")
                    continue
                card = dict(without_server_fields(now), id=becomes)
                if not self.repo.write(becomes, card):
                    run.failed = f"the card {becomes} could not be written"
                    return
                written = self.repo.read(becomes)
                self.journal(run, {"op": "create", "key": becomes, "after": card_revision(written or card)})
                run.created += 1
                self.changed(lock)
            if not self.remove(entry["key"], entry["reason"]):
                run.failed = f"the card {entry['key']} could not be removed"
                return
            self.journal(run, {"op": "remove", "key": entry["key"], "card_id": entry.get("card_id", entry["key"]),
                               "reason": entry["reason"], "keeper": keeper_key or becomes or ""})
            run.removed += 1
            run.removed_keys.append(entry["key"])
            self.changed(lock)

    def verify(self, run: TidyRun, plan: StorePlan, cards: Dict[str, Dict[str, Any]]) -> None:
        """List the store again: every card this run removed is gone, every keeper it updated holds what it joined."""
        after = self.repo.load_all()
        planned = {e["key"] for e in plan.entries}
        for key in run.removed_keys:
            if key in after:
                run.problems.append(f"{key} is still in the store")
        for entry in plan.entries:
            keeper = entry.get("joins") or entry.get("keeper")
            if entry["key"] in run.removed_keys and keeper and keeper in after and keeper not in planned:
                words = {str(k).lower() for k in (after[keeper].get("keywords") or [])}
                missing = [k for k in (cards.get(entry["key"], {}).get("keywords") or [])
                           if str(k).lower() not in words]
                if missing:
                    run.problems.append(f"{keeper} lacks the keywords of {entry['key']}")
        run.verified = not run.problems

    # -- undo -----------------------------------------------------------------------------------

    def undo(self, manifest: Path) -> UndoRun:
        """Take back what a store-tidy manifest records, newest change first, through the port. Never raises."""
        result = UndoRun()
        try:
            lines = [json.loads(raw) for raw in Path(manifest).read_text(encoding="utf-8").splitlines() if raw.strip()]
            header = lines[0]
            exported = json.loads((Path(manifest).parent / header["export"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, IndexError) as exc:
            result.failed = f"the manifest or its export could not be read ({exc})"
            return result
        if header.get("kind") != MANIFEST_KIND:
            result.failed = "this is not a store tidy manifest"
            return result
        if header.get("store") != store_identity(self.repo, self.cards_dir):
            result.failed = "the manifest was written for another store; nothing was changed"
            return result
        lock = self.process_lock()
        if not lock.acquire(blocking=False):
            result.failed = "another process is tidying this store"
            return result
        try:
            for line in reversed([ln for ln in lines[1:] if ln.get("op")]):
                with self.folder_lock() as held:
                    self.undo_one(line, exported, result, held)
                self.pace()
        finally:
            lock.release()
        return result

    def undo_one(self, line: Dict[str, Any], exported: Dict[str, Any], result: UndoRun, lock: Any) -> None:
        key, op = str(line["key"]), line["op"]
        if op == "remove":
            if self.repo.read(key) is not None:
                result.skip("a card is in its place")
                return
            finder = getattr(self.repo, "quarantined", None)
            moved = finder(key) if callable(finder) else None
            if moved is not None:
                os.replace(str(moved), str(self.cards_dir / f"{key}.json"))
            elif card_origin.card_id_problem(key):
                result.skip("its id is refused by every writer, so it stays in the export only")
                return
            elif key not in exported.get("cards", {}):
                result.skip("it is not in the export")
                return
            elif not self.repo.write(key, without_server_fields(exported["cards"][key])):
                result.skip("it could not be written back")
                return
            result.restored += 1
        elif op in ("update", "create"):
            now = self.repo.read(key)
            if now is None or not same_revision(card_revision(now), line.get("after") or {}):
                result.skip("changed since the tidy, so it was left as it is")
                return
            if op == "create":
                if not self.repo.delete(key):
                    result.skip("it could not be removed")
                    return
            elif not self.repo.write(key, without_server_fields(exported["keepers"][key])):
                result.skip("it could not be written back")
                return
            result.reverted += 1
        bump = getattr(lock, "bump", None)
        if callable(bump):
            bump()


def latest_manifest(cards_dir: Any) -> Optional[Path]:
    folder = state_dir_for(cards_dir)
    try:
        found = sorted(folder.glob(f"*{MANIFEST_SUFFIX}"), key=lambda p: p.name)
    except OSError:
        return None
    return found[-1] if found else None


def tidied_message(run: TidyRun, undo_hint: str) -> str:
    return (f"Tidied {run.removed:,} context cards (copies and cards about excluded folders); "
            f"undo with: {undo_hint}")


def planned_message(plan: StorePlan, apply_hint: str) -> str:
    return (f"{len(plan.entries):,} context cards could be tidied ({plan.summary()}); nothing was changed. "
            f"To apply: {apply_hint}")
