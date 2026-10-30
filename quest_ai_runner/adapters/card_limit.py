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
  * a store already ABOVE the limit moves nothing for a learned or quest card (swapping them one for one
    would exchange the regenerable cards for learned ones for good): the card is admitted and counted as kept
    over the limit. The limit never trims such a store: only the tidy (``card_store_hygiene`` for provable
    duplicates, ``card_store_tidy`` through the repository port) removes anything from it.

The limit counts the cards a store OWNS on every repository: a local folder, the Quest backend and Qdrant alike
get the notice and the breakdown. Only a repository that can move a card aside (``quarantine``: a local folder)
ever moves a cold card out for a learned one; on any other, a learned card is admitted over the limit and counted.
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import card_origin
from hubofhubs.recall.card_store_lock import StoreLock
from hubofhubs.filemodes import match_umask

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

# How many cold cards are chosen, before the lock is taken, as the ones that may move out for a learned card.
# Each is read again under the lock, so a few are enough to find one that is still unchanged; this is how many
# are tried, not how many may exist.
VICTIM_CHOICES = 8

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

    ``cards`` returns the store's own cards ``{id: card}`` (parsed; used for the notice and for choosing a card
    to move out). ``repo`` is the store's own repository: a filesystem one can COUNT cards by name and MOVE a
    card (``quarantine``), another one cannot, and then nothing is ever evicted.

    Nothing here does work proportional to the size of the store while the folder's lock is held. The count is
    taken from file names (never from parsing), is cached against the folder's change count, and the cold
    cards to move out are chosen BEFORE the lock is taken; under the lock each is read again, checked again and
    moved, one at a time. A store of 140,000 cards costs a learned write one listing of names, not a parse.
    """

    def __init__(self, cards_dir: str, limit: int, *, repo: Any, cards: Callable[[], Dict[str, Dict[str, Any]]],
                 revision: Callable[[], Any]) -> None:
        self.cards_dir = Path(cards_dir)
        self.limit = max(0, int(limit or 0))
        self.repo = repo
        self.list_cards = cards
        self.revision_of = revision                  # kept for callers; the limit keys on the folder, not on this
        self.lock = threading.RLock()
        self.folder = StoreLock.for_dir(str(cards_dir))      # shared with every process that changes the folder
        self.counted: Optional[int] = None           # how many cards the folder held when last counted
        self.counted_key: Any = None                 # (change count, folder mtime) that count is true for
        self.pending_add = False                     # admit let a new card in; wrote() makes it a fact
        self.folder_changed = False                  # a card was added or moved out by this process
        self.snapshot_cards: Optional[Dict[str, Dict[str, Any]]] = None
        self.snapshot_key: Any = None
        self.announce_due = False
        self.state_dirty = False
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
        self.state_dirty = False
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

    # -- counting ------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def key(self) -> Any:
        """What the folder looks like from outside: the change count every process raises under the lock, and
        the folder's own modification time (which also moves when a person or an older version changed it)."""
        try:
            mtime = self.cards_dir.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if not self.counts_by_name():
            # A repository that is not a folder of card files (the Quest backend, Qdrant) changes without touching
            # the folder: its own change stamp (cheap, cached by the repository) joins the key.
            try:
                return (self.folder.generation(), mtime, self.revision_of())
            except Exception:  # noqa: BLE001
                return (self.folder.generation(), mtime, None)
        return (self.folder.generation(), mtime)

    def counts_by_name(self) -> bool:
        """Whether the repository can count its cards from file names (a local folder store)."""
        return callable(getattr(self.repo, "count", None))

    def count_cards(self) -> int:
        counter = getattr(self.repo, "count", None)
        return int(counter()) if callable(counter) else len(self.list_cards())

    def count(self) -> int:
        """How many cards the store holds: a listing of names, cached until the folder changes."""
        with self.lock:
            key = self.key()
            if self.counted is None or key != self.counted_key:
                self.counted = self.count_cards()
                self.counted_key = key
            return self.counted

    def room(self) -> Optional[int]:
        """How many more derived cards fit, or None with no limit. Zero or less means full."""
        if not self.enabled:
            return None
        return self.limit - self.count()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """The store's own cards, parsed; rebuilt when the folder changed. For the notice and for choosing what moves."""
        with self.lock:
            key = self.key()
            if self.snapshot_cards is None or key != self.snapshot_key:
                self.snapshot_cards = dict(self.list_cards())
                self.snapshot_key = key
            return self.snapshot_cards

    def exists(self, card_id: str) -> bool:
        check = getattr(self.repo, "exists", None)
        if callable(check) and self.counts_by_name():
            return bool(check(card_id))
        return card_id in self.snapshot()

    # -- admission -----------------------------------------------------------------------------

    def prepare(self, card_id: str, card: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
        """BEFORE the folder's lock is taken: the coldest cards that could move out for this one.

        Only a learned or quest card, new to a store that holds exactly its limit, can need room (a card not
        yet built is taken to be a learned one). For everything else (a refresh of a card that exists, a
        derived card, a store with room or already above the limit) this returns ``None`` at once without
        reading a card. A list, possibly empty (every derived card holds something), means "looked, and
        these are the choices".
        """
        if not self.enabled or (card is not None and card_kind(card) in DERIVED_KINDS) or self.exists(card_id):
            return None
        if self.count() != self.limit:
            return None
        return self.coldest(self.snapshot())

    def coldest(self, held: Dict[str, Dict[str, Any]]) -> List[str]:
        """Up to ``VICTIM_CHOICES`` derived cards nobody has touched, coldest first."""
        ranked = heapq.nsmallest(VICTIM_CHOICES, (
            (coldness(cid, c), cid) for cid, c in held.items()
            if card_kind(c) in DERIVED_KINDS and not card_origin.protection(c)))
        return [cid for _, cid in ranked]

    def admit(self, card_id: str, card: Dict[str, Any], victims: Optional[List[str]] = None) -> bool:
        """Whether the store may write ``card`` under ``card_id`` now. The caller holds the folder's lock.

        A card that exists is refreshed. A new card is admitted while there is room; a new derived card is
        refused beyond it. A new learned or quest card, with the store exactly AT its limit, moves one of
        ``victims`` out to make room; with the store already ABOVE its limit it moves nothing (swapping cards
        one for one would only exchange the regenerable ones for learned ones for good) and is counted as kept
        over the limit, as it is when nothing can move. Counts, never parses: see the class.
        """
        if not self.enabled:
            return True
        if self.exists(card_id):
            return True
        with self.lock:
            count = self.count()
            if count < self.limit:
                self.pending_add = True
                return True
            if card_kind(card) in DERIVED_KINDS:
                self.pass_refused.add(card_id)
                self.refused_ids.add(card_id)
                self.reached()
                return False
            if count == self.limit:
                # ``victims`` is None when nobody looked because there was room a moment ago and another
                # process has filled it since: look now (a store at exactly its limit, so this is small).
                choices = victims if victims is not None else self.coldest(self.snapshot())
            else:
                choices = []                     # above the limit: nothing is exchanged for the new card
            if choices and self.make_room(choices, for_card=card_id):
                self.state.evicted += 1
            else:
                self.state.admitted_over += 1
            self.pending_add = True
            self.state_dirty = True
            self.reached()
            return True

    def wrote(self, ok: bool) -> None:
        """The store has written (or failed to write) what ``admit`` allowed: keep the count true.

        Called by the writer while it still holds the folder's lock. A card that joined the store (or left it)
        raises the folder's change count here, once it is really on disk, so every other process counts again.
        """
        if not self.enabled:
            return
        with self.lock:
            added = ok and self.pending_add
            if added and self.counted is not None:
                self.counted += 1
            if added or self.folder_changed:
                self.folder.bump()
            if self.counted is not None:
                self.counted_key = self.key()
            self.pending_add = False
            self.folder_changed = False

    def note_restored(self) -> None:
        """A card the limit moved out came back because a run added to it: it is a kept-over-the-limit card now."""
        with self.lock:
            self.state.admitted_over += 1
            self.reached()

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
                self.state_dirty = True
                if refused or self.pass_unindexed:
                    self.reached()
        self.speak()
        return refused

    def refused_in_pass(self) -> int:
        return len(self.pass_refused)

    def make_room(self, victims: List[str], *, for_card: str) -> bool:
        """Move one of ``victims`` out, coldest first. Each is read again here, under the lock: one that
        another process already moved, or that has since gained notes, usage or tags, is skipped for the next.
        True when a card was moved (by THIS call)."""
        for victim in victims:
            if self.evict(victim, for_card=for_card):
                return True
        return False

    def evict(self, victim: str, *, for_card: str) -> bool:
        move = getattr(self.repo, "quarantine", None)
        read = getattr(self.repo, "read", None)
        if not callable(move) or not callable(read):
            return False
        card = read(victim)
        if not isinstance(card, dict) or card_origin.protection(card):
            return False                         # gone already, or it has gained something: it stays
        if not move(victim, QUARANTINE_REASON):
            return False
        self.folder_changed = True
        if self.counted is not None:
            self.counted -= 1                    # the card is out; the caller's own card goes in next
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
        """Note that the limit has acted; the notice itself is given by ``speak``, outside any lock."""
        if not self.state.reached_at:
            self.state.reached_at = card_origin.now_iso()
        self.state_dirty = True
        self.announce_due = True

    def speak(self) -> None:
        """Give the notice that is due and write the counters, once the caller holds no lock. Never raises.

        Building the notice reads every card, so it is never done while this thread holds the folder's lock
        (a caller that took the lock around its write calls ``speak`` again after it let go)."""
        if self.folder.held_by_current_thread():
            return
        if self.announce_due:
            self.announce_due = False
            self.announce()
        if self.state_dirty:
            self.flush()
        with self.lock:
            if self.counted is not None:
                self.counted_key = self.key()    # the notice and the counters are this process's own files

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
