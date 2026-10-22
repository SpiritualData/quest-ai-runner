"""The card limit: how many cards one store holds, and what happens when it is full.

A store that is allowed to grow without bound did, three times (8,557, 32,070 and 142,781 cards for a
corpus that needs a few hundred). The structural rules in ``card_origin`` and ``context_card_ceilings``
stop each known way in; this is the last line, and it is a NUMBER the owner chose, not one the code
invented: ``QAR_MAX_CARDS``, default 1,000, ``0`` or ``unlimited`` for none. A limit like this was
tried and removed on 2026-10-05 because it silently refused to index and left a person to guess the
number. So the limit never acts without saying so: the first time it bites (or when a store is opened
at or above it) a notice gives the breakdown of what fills the store and how to change the limit
(``card_limit_report``), and ``quest-ai-runner cards report`` prints the same at any time.

AT THE LIMIT, one function (``CardLimit.admit``) is asked by every writer:

  * a card that already exists is refreshed in place, always;
  * a NEW derived card (a topic card or an import; regenerable from the corpus) is not written;
  * a NEW learned card or quest-managed card is admitted, by moving the least valuable derived card
    out to ``_quarantine/limit/`` (unused first, then the least recently touched), so nothing is
    deleted and the total stays where it was. Only a card an index pass wrote and nobody touched can
    move (``card_origin.protection`` is empty: no notes, quest tags, usage, hand-written provenance or
    owner), and it is read again from disk, under the folder's lock, just before it moves. When nothing
    can move, the card is admitted anyway and the notice says so;
  * a store already ABOVE the limit when opened is never trimmed by the limit: only the checks for
    provable duplicates (``card_store_hygiene``) remove anything from it.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import card_origin
from .card_store_lock import StoreLock
from ..core.file_modes import match_umask

log = logging.getLogger("quest-ai-runner.card-limit")

ENV_MAX_CARDS = "QAR_MAX_CARDS"
DEFAULT_MAX_CARDS = 1000
# Words that mean "no limit" in ``QAR_MAX_CARDS`` besides ``0``.
NO_LIMIT_WORDS = frozenset({"0", "none", "off", "no", "unlimited", "nolimit", "no-limit"})
STATE_FILE = "index-state/card_limit.json"
QUARANTINE_REASON = "limit"
MANIFEST_NAME = "manifest.jsonl"

KIND_TOPIC = "topic"
KIND_IMPORTED = "imported"
KIND_LEARNED = "learned"
KIND_MANAGED = "managed"
DERIVED_KINDS = frozenset({KIND_TOPIC, KIND_IMPORTED})

announced_stores: Set[str] = set()
announce_lock = threading.Lock()
# Notices a lane has no screen for, waiting for the next task's progress feed: {card folder: one line}.
task_notes: Dict[str, str] = {}


def hold_task_note(cards_dir: str, text: str) -> None:
    """Keep ``text`` for the next task that starts in this process (see ``take_task_notes``)."""
    with announce_lock:
        task_notes[cards_dir] = text


def take_task_notes() -> List[str]:
    """The notices held for a task's progress feed, each handed out once. Never raises."""
    with announce_lock:
        notes = list(task_notes.values())
        task_notes.clear()
    return notes


def max_cards_from_env(env: Optional[Dict[str, str]] = None) -> int:
    """The configured card limit: ``QAR_MAX_CARDS``, else 1,000. ``0`` means no limit. Never raises."""
    raw = str((env if env is not None else os.environ).get(ENV_MAX_CARDS) or "").strip().lower()
    if not raw:
        return DEFAULT_MAX_CARDS
    if raw in NO_LIMIT_WORDS:
        return 0
    try:
        value = int(raw.replace(",", "").replace("_", ""))
    except ValueError:
        log.warning("%s=%r is not a number; using the default of %d. Use a whole number, or 0 for no limit.",
                    ENV_MAX_CARDS, raw, DEFAULT_MAX_CARDS)
        return DEFAULT_MAX_CARDS
    return value if value >= 0 else DEFAULT_MAX_CARDS


def card_kind(card: Dict[str, Any]) -> str:
    """What a card is: a quest-managed one, a copy imported from another store, a topic card written
    from the corpus, or one learned from a run (anything else)."""
    if card.get("managed_by"):
        return KIND_MANAGED
    if card_origin.is_copy(card):
        return KIND_IMPORTED
    if str(card_origin.provenance_of(card).get("created_by_task") or "") == "bootstrap":
        return KIND_TOPIC
    return KIND_LEARNED


def reset_announcements() -> None:
    """Forget which stores this process has already announced (for tests)."""
    with announce_lock:
        announced_stores.clear()
        task_notes.clear()


@dataclass
class LimitState:
    """What the limit has done to a store since it was first reached (persisted beside the cards)."""

    reached_at: str = ""
    waiting: int = 0            # new derived cards refused in the latest pass (distinct ids)
    unindexed_files: int = 0    # files without a card in the latest pass because there was no room
    evicted: int = 0            # derived cards moved out for a learned or quest card (all time)
    admitted_over: int = 0      # learned or quest cards kept above the limit (all time)
    last_notice_at: str = ""


def coldness(card_id: str, card: Dict[str, Any]):
    """Sort key, coldest first: unused before used, then least recently touched, then oldest."""
    from .context_store_cards import card_last_touched  # local: that module is heavy and imports this one's siblings
    try:
        used = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        used = 0
    created = str(card_origin.provenance_of(card).get("created_at") or "")
    return (used, card_last_touched(card), created or "~", card_id)


class CardLimit:
    """Admission control for the cards one store writes (see the module docstring).

    ``cards`` returns the store's own cards ``{id: card}``; ``revision`` is the repository's cheap
    change stamp, so the snapshot is rebuilt only when somebody else wrote. ``repo`` is the store's
    own repository: a filesystem one can MOVE a card (``quarantine``), another one cannot, and then
    nothing is ever evicted.
    """

    def __init__(self, cards_dir: str, limit: int, *, repo: Any, cards: Callable[[], Dict[str, Dict[str, Any]]],
                 revision: Callable[[], Any]) -> None:
        self.cards_dir = Path(cards_dir)
        self.limit = max(0, int(limit or 0))
        self.repo = repo
        self.list_cards = cards
        self.revision_of = revision
        self.lock = threading.RLock()
        self.folder = StoreLock.for_dir(str(cards_dir))      # shared with every process that changes the folder
        self.generation_seen = -1                            # the folder's change count the snapshot is of
        self.membership_changed = False                      # admit let a new card in (or moved one out)
        self.snapshot_cards: Optional[Dict[str, Dict[str, Any]]] = None
        self.stamp: Any = None
        self.state = self.load_state()
        self.refused_ids: Set[str] = set()      # every id refused by this process, for tests and callers
        self.pass_refused: Set[str] = set()
        self.pass_unindexed = 0

    # -- state ------------------------------------------------------------------------------

    def state_path(self) -> Path:
        return self.cards_dir / STATE_FILE

    def load_state(self) -> LimitState:
        try:
            data = json.loads(self.state_path().read_text(encoding="utf-8"))
            return LimitState(**{k: v for k, v in data.items() if k in LimitState.__dataclass_fields__})
        except Exception:  # noqa: BLE001
            return LimitState()

    def flush(self) -> None:
        """Write the counters beside the cards. Best effort; never raises."""
        try:
            target = self.state_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_limit_", suffix=".json")
            match_umask(fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(asdict(self.state), fh, indent=2)
                    fh.write("\n")
                os.replace(tmp, str(target))
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            log.debug("could not write the card limit state", exc_info=True)

    # -- the snapshot --------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """The store's own cards, rebuilt when somebody else changed the repository.

        "Somebody else" is known two ways: the repository's change stamp, and the folder lock's change
        count, which every process raises when it adds or moves a card. The second is exact, so a
        writer in another process can never leave this one counting a store that has since changed.
        """
        with self.lock:
            stamp = self.revision_of()
            generation = self.folder.generation()
            if self.snapshot_cards is None or stamp != self.stamp or generation != self.generation_seen:
                self.snapshot_cards = dict(self.list_cards())
                self.stamp = stamp
                self.generation_seen = generation
            return self.snapshot_cards

    def count(self) -> int:
        return len(self.snapshot())

    def room(self) -> Optional[int]:
        """How many more derived cards fit, or None with no limit. Zero or less means full."""
        if not self.enabled:
            return None
        return self.limit - self.count()

    # -- admission -----------------------------------------------------------------------------

    def admit(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Whether the store may write ``card`` under ``card_id`` now. Counts and announces refusals."""
        if not self.enabled:
            return True
        with self.folder, self.lock:
            held = self.snapshot()
            if card_id in held:
                return True
            if len(held) < self.limit:
                held[card_id] = card
                self.membership_changed = True
                return True
            kind = card_kind(card)
            if kind in DERIVED_KINDS:
                self.pass_refused.add(card_id)
                self.refused_ids.add(card_id)
                self.reached()
                return False
            if self.make_room(held, for_card=card_id):
                self.state.evicted += 1
            else:
                self.state.admitted_over += 1
            held[card_id] = card
            self.membership_changed = True
            # The move changed the folder; the snapshot in hand is right for it (it has the card that is
            # about to be written), so it must not be rebuilt from disk by the notice below.
            self.stamp = self.revision_of()
            self.reached()
            self.flush()
            return True

    def wrote(self, ok: bool) -> None:
        """The store has written (or failed to write) what ``admit`` allowed: keep the snapshot true.

        Called by the writer while it still holds the folder's lock. A card that joined the store (or
        left it) raises the folder's change count here, once it is really on disk, so every other
        process looks at the folder again before it counts it.
        """
        if not self.enabled:
            return
        with self.folder, self.lock:
            if ok:
                self.stamp = self.revision_of()
                if self.membership_changed:
                    self.generation_seen = self.folder.bump() or self.generation_seen
            else:
                self.snapshot_cards = None
            self.membership_changed = False

    def begin_pass(self) -> None:
        """An indexing pass starts: what it refuses is counted afresh."""
        with self.lock:
            self.pass_refused = set()
            self.pass_unindexed = 0

    def note_unindexed(self, files: int) -> None:
        """Files this pass left without a card because the store was full (no card could hold them).

        Counted only: the notice is given when the pass ends (``end_pass``), once the cards it did write are
        in the store, so what it says the store holds is true."""
        if files > 0:
            with self.lock:
                self.pass_unindexed += files

    def end_pass(self) -> int:
        """The pass is over: record what is waiting for room. Returns how many cards it refused."""
        with self.lock:
            refused = len(self.pass_refused)
            if refused or self.pass_unindexed or self.state.waiting or self.state.unindexed_files:
                self.state.waiting = refused
                self.state.unindexed_files = self.pass_unindexed
                if refused or self.pass_unindexed:
                    self.reached()
                self.flush()
            return refused

    def refused_in_pass(self) -> int:
        return len(self.pass_refused)

    def least_valuable_derived(self, held: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """The coldest derived card nobody has touched, or None when every derived card holds something."""
        candidates = [(coldness(cid, c), cid) for cid, c in held.items()
                      if card_kind(c) in DERIVED_KINDS and not card_origin.protection(c)]
        return min(candidates)[1] if candidates else None

    def make_room(self, held: Dict[str, Dict[str, Any]], *, for_card: str) -> bool:
        """Move one cold derived card out. Each candidate is read again first: a card another process
        already moved, or one that has since gained notes, usage or tags, is dropped from the choice and
        the next coldest is tried. True when a card was moved (by THIS call)."""
        tried: Set[str] = set()
        while True:
            candidates = {cid: c for cid, c in held.items() if cid not in tried}
            victim = self.least_valuable_derived(candidates)
            if victim is None:
                return False
            tried.add(victim)
            if self.evict(victim, held, for_card=for_card):
                return True

    def evict(self, victim: str, held: Dict[str, Dict[str, Any]], *, for_card: str) -> bool:
        move = getattr(self.repo, "quarantine", None)
        read = getattr(self.repo, "read", None)
        if not callable(move) or not callable(read):
            return False
        card = read(victim)
        if not isinstance(card, dict):
            held.pop(victim, None)              # somebody else moved it already: not ours to count
            return False
        if card_origin.protection(card):
            held[victim] = card                 # it has gained something since the snapshot: it stays
            return False
        if not move(victim, QUARANTINE_REASON):
            return False
        held.pop(victim, None)
        self.append_manifest({"moved": victim, "kind": card_kind(card), "for": for_card,
                               "usage_count": card.get("usage_count", 0), "at": card_origin.now_iso(),
                               "why": "the store was at its card limit and a card learned from a run or "
                                      "written for a quest needed the room"})
        return True

    def append_manifest(self, line: Dict[str, Any]) -> None:
        try:
            folder = self.cards_dir / "_quarantine" / QUARANTINE_REASON
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / MANIFEST_NAME, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("could not write the card limit manifest", exc_info=True)

    # -- saying so -------------------------------------------------------------------------------

    def reached(self) -> None:
        if not self.state.reached_at:
            self.state.reached_at = card_origin.now_iso()
        self.announce()

    def announce(self, *, at_open: bool = False, notify: Optional[Callable[[str], None]] = None) -> bool:
        """Say, once per process, that this store is full, with what fills it. True when it spoke."""
        key = str(self.cards_dir.resolve())
        with announce_lock:
            if key in announced_stores:
                return False
            announced_stores.add(key)
        try:
            from .card_limit_report import announce_full
            announce_full(self, at_open=at_open, notify=notify)
        except Exception:  # noqa: BLE001 -- the limit must never fail a write because it could not talk
            log.warning("Your context store is at its card limit of %d cards.", self.limit, exc_info=True)
        self.state.last_notice_at = card_origin.now_iso()
        self.flush()
        return True

    def check_at_open(self, notify: Optional[Callable[[str], None]] = None) -> bool:
        """At open: say so when the store is already at or above its limit. Nothing is removed."""
        if self.enabled and self.count() >= self.limit:
            if not self.state.reached_at:
                self.state.reached_at = card_origin.now_iso()
            return self.announce(at_open=True, notify=notify)
        return False
