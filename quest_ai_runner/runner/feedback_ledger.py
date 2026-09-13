"""What a person asked for, and how far anybody has actually got with it.

Every channel in ``runner/context_updates.py`` answers "what has arrived since an assistant last
looked". This module answers the question that outlives it: **what became of it**. A note, a
document comment and a capture are all somebody asking for something, and until now the only
record that any of them had been handled was circumstantial -- an assistant note appearing after a
person's note, a reply sitting under their comment, a run finishing later that same afternoon.

CIRCUMSTANCE IS NOT A STATUS, and the difference is not academic. "A run delivered a result after
your note" closes a note whether the run read it, half-did it, or never mentioned it. A person who
writes "from now on, put the page numbers in" and gets a reply that afternoon has their instruction
marked handled forever, on the strength of the timestamp, while the page numbers are still missing
from every document written since. That is the failure this module exists to end: **replying is not
doing**, and the record has to know the difference.

WHO SETS THE STATUS. The run does, in its own words, in the receipt it already writes
(``usage_receipt_gate``). It is the only participant that knows what it did, and asking a second
model to grade the first would be both dearer and less true. What changed is that the receipt line
now opens with a DISPOSITION from a fixed, listed vocabulary, so an account can be recorded rather
than only displayed. That is a structured decision the run is asked to make, not a scan of its
prose for interesting words -- a disposition this module does not recognise leaves the item exactly
as it was, which is the safe direction, since an unrecorded answer costs a person one repeated
question and a wrongly recorded one costs them the request.

A PERSON ALWAYS OUTRANKS A RUN. A status a human set is locked: no run's disposition may overwrite
it. Somebody saying "no, this still is not done" has to stick, or the ledger is just the assistant
marking its own homework.

TWO KINDS OF ASK, WHICH IS THE WHOLE REASON THIS IS NOT SIMPLY A DONE FLAG.

* A **request** is finished once: "add a Status column to the limitations sheet". It goes
  ``open -> in progress -> done`` and then it is over.
* A **standing rule** is never finished: "from now on include what you are doing on limitations
  gathering in every report". Marking it done the first time it is honoured is exactly how a
  standing rule quietly stops being followed: the record says handled, so nothing ever raises it
  again, and the third report drops it with nobody the wiser. A standing rule instead stays IN
  FORCE, and carries how far it has got: ``partially applied`` when only part of it has been done,
  ``needs reapplying`` when it has lapsed or the person has had to say it twice.

AND STANDING RULES ARE GUIDANCE. A rule that governs future work is precisely what a guidance card
is (``adapters/guidance_card_manager.py``, ``docs/guidance-cards.md``), so an item the run marks
standing is offered to whatever guidance writer the deployment wired, in the person's own words,
with the item id on it. The two halves then do different jobs and stop duplicating each other: the
CARD carries the rule into every future run through retrieval, and the LEDGER carries whether it is
actually being followed. Neither can do both. A card with no status cannot tell you a rule is being
half-followed; a ledger with no card cannot get the rule in front of tomorrow's run.

The store is a JSON file beside the lane's own state, same shape and same atomic write as
``Watermarks``, so a lane needs no database to keep this. A deployment with somewhere better to put
it supplies its own store: this module is duck-typed on ``load``/``save`` and nothing else.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple)

try:                                  # POSIX: the cross-process lock under a shared ledger file
    import fcntl
except ImportError:                   # pragma: no cover -- Windows: single-process behaviour
    fcntl = None                      # type: ignore[assignment]

log = logging.getLogger("quest-ai-runner.feedback-ledger")


# ---------------------------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------------------------

# What KIND of ask this is. The distinction a plain done-flag cannot express.
KIND_REQUEST = "request"      # finished once
KIND_STANDING = "standing"    # governs future work, never "done"
KIND_CONTEXT = "context"      # nothing to do; they were telling you something
KIND_QUESTION = "question"    # owed an ANSWER from a person; authorises nothing on its own
KIND_UNKNOWN = "unknown"      # nobody has said yet

# ``unknown`` + ``open`` is a row that exists and has not been triaged: it is what an automatic
# capture (a monitored mailbox, an inbound message) creates. It is deliberately not a state of its
# own, because "captured" is not a stage of an ask, it is the absence of anybody having classified
# it yet, and a row nobody has classified must never be read as an instruction to act.

# What STATE it is in. Everything in ``OPEN_STATES`` is still owed something.
OPEN = "open"                            # nobody has acted on it
IN_PROGRESS = "in progress"              # begun, not finished
DONE = "done"                            # a request, finished
IN_FORCE = "in force"                    # a standing rule, live and followed so far
PARTIALLY_APPLIED = "partially applied"  # a standing rule, only part of it done so far
NEEDS_REAPPLYING = "needs reapplying"    # a standing rule that lapsed, or was said twice
DECLINED = "declined"                    # deliberately not doing it (the reason is the note)
NOTED = "noted"                          # context, acknowledged, nothing owed
SUPERSEDED = "superseded"                # replaced by something the person said later
BLOCKED = "blocked"                      # begun and stuck on something outside the run
AWAITING_ANSWER = "awaiting answer"      # a question put TO a person, not yet answered
AWAITING_ACCEPTANCE = "awaiting acceptance"  # a run says it finished; no person has agreed yet
ACCEPTED = "accepted"                    # a PERSON confirmed it is finished. Only they can.

# The states that still want a run's attention. ``IN_FORCE`` is deliberately NOT here: a rule that
# is being followed does not need re-announcing every morning, it needs to be in the guidance the
# next run retrieves. It re-enters this set the moment it is only partly applied or has lapsed.
#
# ``AWAITING_ACCEPTANCE`` IS owed, and that is the point of it: a run saying it has finished is a
# claim, not a conclusion, and an item that leaves the owed set on the strength of the claim is the
# assistant marking its own homework one step later than "replying is not doing". It leaves the set
# when a person accepts it, and not before. ``AWAITING_ANSWER`` and ``BLOCKED`` are owed for the
# plainer reason that somebody is waiting on somebody.
OPEN_STATES = frozenset({OPEN, IN_PROGRESS, PARTIALLY_APPLIED, NEEDS_REAPPLYING,
                         BLOCKED, AWAITING_ANSWER, AWAITING_ACCEPTANCE})

# Dispositions a run may declare on a receipt line, and what each one means. This IS the list the
# run is shown (see ``disposition_vocabulary``), so it is written for a reader, not a parser.
DISPOSITIONS: Dict[str, str] = {
    "done": "a one-time request, and you finished it",
    "partial": "you worked on it but it is not finished; it stays open",
    "standing": "an instruction for future work, not a one-off; you followed it this time",
    "standing-partial": "an instruction for future work that you have only been able to follow "
                        "in part so far",
    "asked": "you put a question back to them and are waiting on their answer; you did NOT act "
             "on it and must not act until they reply",
    "blocked": "you started and are stuck on something outside your control; say what on",
    "declined": "deliberately not doing it, for the reason you give",
    "noted": "they were telling you something; there is nothing to do",
    "not used": "you did not use it; it stays exactly as it was",
}

# disposition -> (kind, state). ``not used`` is absent on purpose: it changes nothing.
_TRANSITIONS: Dict[str, Tuple[str, str]] = {
    "done": (KIND_REQUEST, DONE),
    "partial": (KIND_REQUEST, IN_PROGRESS),
    "standing": (KIND_STANDING, IN_FORCE),
    "standing-partial": (KIND_STANDING, PARTIALLY_APPLIED),
    "asked": (KIND_QUESTION, AWAITING_ANSWER),
    # Blocked says nothing about what KIND of ask it is, so it leaves the kind alone: a standing
    # rule that is stuck is still a standing rule.
    "blocked": ("", BLOCKED),
    "declined": (KIND_UNKNOWN, DECLINED),
    "noted": (KIND_CONTEXT, NOTED),
}

# How many entries of an item's own history to keep. Enough to see the shape of a long-running
# standing rule; bounded so one item cannot grow the store without limit.
MAX_HISTORY = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: Optional[datetime]) -> str:
    return when.astimezone(timezone.utc).isoformat() if when else ""


def _parse(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_disposition(raw: Any) -> str:
    """A disposition as this module knows it, or "" for anything it does not.

    Lenient about the shapes a run will really write ("Done", "standing rule", "partially"), strict
    about the result: either it is one of ``DISPOSITIONS`` or it is nothing, and nothing changes
    an item's state. There is no fuzzy middle where a half-understood word half-closes a request.
    """
    text = " ".join(str(raw or "").strip().lower().replace("_", "-").split())
    if not text:
        return ""
    if text in DISPOSITIONS:
        return text
    aliases = {
        "not-used": "not used", "unused": "not used", "no": "not used", "none": "not used",
        "complete": "done", "completed": "done", "finished": "done", "fixed": "done",
        "partially": "partial", "in progress": "partial", "started": "partial",
        "standing rule": "standing", "ongoing": "standing", "rule": "standing",
        "standing partial": "standing-partial", "partially applied": "standing-partial",
        "acknowledged": "noted", "noted only": "noted", "fyi": "noted",
        "question": "asked", "asking": "asked", "awaiting answer": "asked",
        "asked them": "asked", "waiting on them": "asked",
        "stuck": "blocked", "blocked on": "blocked", "waiting on": "blocked",
        "declining": "declined", "refused": "declined", "will not do": "declined",
    }
    return aliases.get(text, "")


def disposition_vocabulary() -> str:
    """The list of dispositions, written out for the run that has to choose one."""
    return "\n".join(f"  {name:<17} {meaning}" for name, meaning in DISPOSITIONS.items())


# ---------------------------------------------------------------------------------------------
# The item
# ---------------------------------------------------------------------------------------------

@dataclass
class FeedbackItem:
    """One thing a person asked for, wherever they said it, and where it has got to.

    ``key`` is card + source + the source's own id, so the same note is one row however many times
    it is offered, and two quests watching the same document keep their own records of what each
    has done about a comment.
    """
    key: str = ""
    card_id: str = ""
    source: str = ""
    item_id: str = ""
    author: str = ""
    text: str = ""                       # their words, kept verbatim; the rule IS what they wrote
    location: str = ""
    url: str = ""
    occurred_at: str = ""                # when they said it

    kind: str = KIND_UNKNOWN
    state: str = OPEN
    # Whether a PERSON set the current state. A run may not overwrite it.
    set_by_person: bool = False

    first_seen_at: str = ""
    last_offered_at: str = ""            # last time it was put in front of a run
    last_applied_at: str = ""            # last time a run said it acted on it
    applications: int = 0                # how many times a run has acted on it
    last_note: str = ""                  # the run's own words, most recent
    # What backs the most recent claim: a URL, a message id, a commit, a file path. Kept as the
    # refs themselves rather than prose, because a claim a person cannot go and check is the same
    # unverifiable assertion whether it is one sentence or ten.
    evidence: List[str] = field(default_factory=list)
    last_run_id: str = ""                # who made that claim, when the caller knows
    guidance_card_id: str = ""           # set when a standing rule was written to guidance
    history: List[Dict[str, str]] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        """Whether this still wants a run's attention."""
        return self.state in OPEN_STATES

    @property
    def is_standing(self) -> bool:
        return self.kind == KIND_STANDING

    def status_line(self) -> str:
        """How this item's state reads to the person who asked, in their terms.

        A standing rule says how far it has got, because "in force" on its own is the claim a
        person has no way to check and every reason to doubt.
        """
        if self.state == IN_FORCE:
            seen = f", last applied {self.last_applied_at[:10]}" if self.last_applied_at else ""
            return f"standing instruction, in force{seen}"
        if self.state == PARTIALLY_APPLIED:
            return (f"standing instruction, applied only in part so far"
                    f"{f' ({self.last_note})' if self.last_note else ''}")
        if self.state == NEEDS_REAPPLYING:
            return "standing instruction that has not been applied since it was last asked for"
        if self.state == AWAITING_ACCEPTANCE:
            who = f" by {self.last_run_id}" if self.last_run_id else ""
            backing = f"; evidence: {', '.join(self.evidence)}" if self.evidence else ""
            return f"reported finished{who}, waiting on a person to accept it{backing}"
        if self.state == ACCEPTED:
            return f"accepted {self.last_applied_at[:10]}" if self.last_applied_at else "accepted"
        if self.state == AWAITING_ANSWER:
            return (f"a question was put back to them and is unanswered"
                    f"{f' ({self.last_note})' if self.last_note else ''}")
        if self.state == BLOCKED:
            return f"blocked{f': {self.last_note}' if self.last_note else ''}"
        if self.state == IN_PROGRESS:
            return f"started, not finished{f' ({self.last_note})' if self.last_note else ''}"
        if self.state == DONE:
            return f"done {self.last_applied_at[:10]}" if self.last_applied_at else "done"
        if self.state == DECLINED:
            return f"declined{f': {self.last_note}' if self.last_note else ''}"
        if self.state == NOTED:
            return "noted, nothing to do"
        if self.state == SUPERSEDED:
            return "superseded by something said later"
        return "waiting on an answer"

    @property
    def authorizes_execution(self) -> bool:
        """Whether anything may be DONE about this item without asking a person first.

        Deliberately conservative, and deliberately not a judgment about the words: an untriaged
        row (an automatic capture nobody has classified), an unanswered question, and anything a
        person has already settled all answer no. Capture is how something gets LOOKED at, never
        how it gets acted on, or a monitored mailbox becomes a queue of instructions nobody issued.
        """
        if self.kind == KIND_UNKNOWN or self.kind == KIND_QUESTION:
            return False
        return self.state in OPEN_STATES and self.state != AWAITING_ACCEPTANCE

    def record(self, *, state: str, kind: str = "", note: str = "",
               by: str = "run", at: Optional[datetime] = None,
               evidence: Sequence[str] = (), run_id: str = "") -> None:
        """Move this item, and keep the account of who moved it and why."""
        when = at or _utcnow()
        self.state = state
        if kind:
            self.kind = kind
        if note:
            self.last_note = note
        if evidence:
            self.evidence = [str(e) for e in evidence if str(e).strip()]
        if run_id:
            self.last_run_id = str(run_id)
        if by == "person":
            self.set_by_person = True
        self.history.append({"at": _iso(when), "state": state, "by": by, "note": note})
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]


# ---------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------

class FeedbackLedger:
    """Every tracked item for this lane, JSON-backed.

    Deliberately the same shape as ``Watermarks``: one small file beside the lane's state, written
    atomically, degrading to in-memory when given no path. A lane should not need a database to
    remember that somebody is still waiting on something.
    """

    def __init__(self, path: Optional[str] = None, *, read_only: bool = False,
                 requires_acceptance: bool = False) -> None:
        self._path = Path(path) if path else None
        self._read_only = bool(read_only)
        # Whether a run may finish something on its own say-so. A lane whose asks are its own work
        # leaves this False and behaves exactly as it always has. A consumer whose asks come from
        # PEOPLE (a cockpit, a mailbox) sets it True, and then a run's ``done`` reaches only
        # ``awaiting acceptance``: the claim is recorded, the item stays owed, and a person closes
        # it. Nothing else about the vocabulary changes, which is why this is a flag and not a
        # second class.
        self._requires_acceptance = bool(requires_acceptance)
        self._items: Dict[str, FeedbackItem] = {}
        self._lock = threading.Lock()
        self._stamp: Tuple[int, int] = (-1, -1)
        self._load()

    # --- persistence ---------------------------------------------------------------------

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def requires_acceptance(self) -> bool:
        return self._requires_acceptance

    @staticmethod
    def key_for(card_id: str, source: str, item_id: str) -> str:
        return f"{card_id}|{source}|{item_id}"

    def _file_stamp(self) -> Tuple[int, int]:
        """A cheap "has the file changed" fingerprint: (mtime_ns, size)."""
        try:
            st = self._path.stat() if self._path else None
        except OSError:
            return (-1, -1)
        return (st.st_mtime_ns, st.st_size) if st else (-1, -1)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold this lane's ledger against every other thread AND every other process.

        The store is shared: a poller and a consumer's own backend can both hold one, on the same
        file, in different processes, and the base guard is a ``threading.Lock``, which one process
        cannot see from another. Two writers with an in-memory copy each would then take turns
        rewriting the whole file from a stale dict, and the loser's rows would vanish with no error
        anywhere. So a write takes an OS lock on a neighbouring lockfile and RE-READS the file
        inside it: read, modify, write, release, every time. It costs one small read per write, and
        it is the difference between a shared record and two processes overwriting each other.
        """
        with self._lock:
            if self._path is None:
                yield
                return
            handle = None
            try:
                if fcntl is None:
                    raise OSError("no fcntl on this platform")
                lock_path = self._path.with_suffix(self._path.suffix + ".lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(lock_path, "a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as e:            # no flock here (a share, an odd filesystem): carry on
                log.debug("feedback ledger: no cross-process lock (%s)", e)
                handle = None
            try:
                self._reload_if_changed()
                yield
            finally:
                if handle is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()

    @contextmanager
    def _fresh(self) -> Iterator[None]:
        """A read that sees what another process has written since the last one."""
        with self._lock:
            self._reload_if_changed()
            yield

    def _reload_if_changed(self) -> None:
        """Re-read the file when it has moved on. Call inside ``self._lock``."""
        if not self._path:
            return
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return
        self._items = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        self._stamp = self._file_stamp()
        try:
            payload = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("feedback ledger unreadable (%s); starting empty", e)
            return
        rows = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(rows, dict):
            return
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            fields = {k: v for k, v in row.items() if k in FeedbackItem.__annotations__}
            fields["key"] = str(key)
            try:
                self._items[str(key)] = FeedbackItem(**fields)
            except TypeError:          # a row written by a newer version: skip, never crash
                continue

    def _save(self) -> None:
        if not self._path or self._read_only:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"items": {k: asdict(v) for k, v in sorted(self._items.items())}},
                indent=2, sort_keys=False))
            os.replace(tmp, self._path)    # atomic: never a partial file after a crash
            # Our own write is not a reason to re-read on the next call.
            self._stamp = self._file_stamp()
        except OSError as e:
            log.warning("could not persist the feedback ledger: %s", e)

    # --- reads ---------------------------------------------------------------------------

    def get(self, card_id: str, source: str, item_id: str) -> Optional[FeedbackItem]:
        with self._fresh():
            return self._items.get(self.key_for(card_id, source, item_id))

    def for_card(self, card_id: str) -> List[FeedbackItem]:
        with self._fresh():
            return [i for i in self._items.values() if i.card_id == card_id]

    def all_items(self) -> List[FeedbackItem]:
        """Every row in this store, whatever card it belongs to, oldest first.

        For a consumer whose ledger is scoped by its own path (one per person, one per mailbox)
        rather than by card: it holds the file, so "everything in it" is the question it actually
        has, and reconstructing that from a card list it would have to keep separately is how two
        records of the same thing start to disagree.
        """
        with self._fresh():
            rows = list(self._items.values())
        rows.sort(key=lambda i: i.occurred_at or i.first_seen_at)
        return rows

    def open_items(self, card_id: str) -> List[FeedbackItem]:
        """Everything on this card that still owes somebody something, oldest first."""
        rows = [i for i in self.for_card(card_id) if i.is_open]
        rows.sort(key=lambda i: i.occurred_at or i.first_seen_at)
        return rows

    def standing_rules(self, card_id: str) -> List[FeedbackItem]:
        """The standing instructions on this card, whatever their state of application."""
        rows = [i for i in self.for_card(card_id) if i.is_standing]
        rows.sort(key=lambda i: i.occurred_at or i.first_seen_at)
        return rows

    # --- writes --------------------------------------------------------------------------

    def observe(self, *, card_id: str, source: str, item_id: str, text: str = "",
                author: str = "", location: str = "", url: str = "",
                occurred_at: Optional[datetime] = None,
                at: Optional[datetime] = None) -> FeedbackItem:
        """Register that this item exists and was put in front of a run. Never changes its state.

        Seeing something is not acting on it, so this only ever creates a row (open, kind unknown)
        or stamps ``last_offered_at`` on one that already exists. The separation is the point: the
        engine can then tell "offered three times, still open" from "never reached a run", which
        are different problems with different fixes.
        """
        when = at or _utcnow()
        key = self.key_for(card_id, source, item_id)
        with self._exclusive():
            item = self._items.get(key)
            if item is None:
                item = FeedbackItem(
                    key=key, card_id=card_id, source=source, item_id=item_id,
                    author=author, text=text, location=location, url=url,
                    occurred_at=_iso(occurred_at), first_seen_at=_iso(when),
                )
                self._items[key] = item
            else:
                # Their words can be edited after the fact (a comment, a note); keep the latest.
                if text:
                    item.text = text
                if location and not item.location:
                    item.location = location
            item.last_offered_at = _iso(when)
            self._save()
            return item

    def apply_disposition(self, *, card_id: str, source: str, item_id: str,
                          disposition: str, note: str = "",
                          at: Optional[datetime] = None,
                          by: str = "run",
                          evidence: Sequence[str] = (),
                          run_id: str = "") -> Optional[FeedbackItem]:
        """Record what a run said it did, and move the item accordingly.

        Returns the item, or None when there is nothing to move (an unknown item, or a disposition
        this module does not recognise). Unrecognised leaves the item untouched ON PURPOSE: the
        cost of not recording an answer is one repeated question, and the cost of recording one
        wrongly is a request that silently stops being anybody's problem.
        """
        clean = normalize_disposition(disposition)
        transition = _TRANSITIONS.get(clean)
        if transition is None:
            return None
        when = at or _utcnow()
        key = self.key_for(card_id, source, item_id)
        with self._exclusive():
            item = self._items.get(key)
            if item is None:
                return None
            # A person's word stands until that person changes it.
            if item.set_by_person and by != "person":
                log.info("feedback ledger: %s is held at %r by a person; a run's %r is not applied",
                         key, item.state, clean)
                return item
            kind, state = transition
            # A run acting on a standing rule that was already recorded standing must not demote
            # it to a request just because it wrote "done" on the day it happened to comply.
            if item.is_standing and kind == KIND_REQUEST:
                state = IN_FORCE if state == DONE else PARTIALLY_APPLIED
                kind = KIND_STANDING
            # Where the asks come from people, a run's "done" is a CLAIM. It is recorded as one,
            # with whatever backs it, and the item stays owed until a person accepts it.
            if state == DONE and self._requires_acceptance and by != "person":
                state = AWAITING_ACCEPTANCE
            if state in (IN_FORCE, PARTIALLY_APPLIED, DONE, IN_PROGRESS, AWAITING_ACCEPTANCE):
                item.applications += 1
                item.last_applied_at = _iso(when)
            item.record(state=state, kind=kind, note=note, by=by, at=when,
                        evidence=evidence, run_id=run_id)
            self._save()
            return item

    def set_state(self, *, card_id: str, source: str, item_id: str, state: str,
                  kind: str = "", note: str = "", by: str = "person",
                  at: Optional[datetime] = None,
                  evidence: Sequence[str] = (), run_id: str = "") -> Optional[FeedbackItem]:
        """Set an item's state directly, and optionally say what kind of ask it is.

        ``by="person"`` locks the state against any run. ``kind`` is how an untriaged row (an
        automatic capture, kind ``unknown``) becomes a request, a question or a standing rule:
        classification is a person's call, and until they make it the row authorises nothing.
        """
        key = self.key_for(card_id, source, item_id)
        with self._exclusive():
            item = self._items.get(key)
            if item is None:
                return None
            item.record(state=state, kind=kind, note=note, by=by, at=at,
                        evidence=evidence, run_id=run_id)
            self._save()
            return item

    def accept(self, *, card_id: str, source: str, item_id: str, note: str = "",
               by: str = "person", at: Optional[datetime] = None) -> Optional[FeedbackItem]:
        """A PERSON agrees this is finished. The only route to ``accepted``.

        There is no disposition a run can write that reaches this state, and this refuses to be
        called on a run's behalf, because an assistant that can accept its own work has a done-flag
        with extra steps. Accepting locks the item: a later run cannot reopen what a person closed.
        """
        if by != "person":
            log.info("feedback ledger: %s|%s|%s -- only a person may accept; %r refused",
                     card_id, source, item_id, by)
            return None
        when = at or _utcnow()
        key = self.key_for(card_id, source, item_id)
        with self._exclusive():
            item = self._items.get(key)
            if item is None:
                return None
            item.last_applied_at = _iso(when)
            item.record(state=ACCEPTED, note=note, by="person", at=when)
            self._save()
            return item

    def reopen(self, *, card_id: str, source: str, item_id: str, note: str = "",
               at: Optional[datetime] = None) -> Optional[FeedbackItem]:
        """The person came back to it: a standing rule needs reapplying, anything else is open.

        What "came back to it" means is each channel's own business (a new reply under an answered
        comment, a person restating an instruction). This only says what that means for the record.
        """
        key = self.key_for(card_id, source, item_id)
        with self._exclusive():
            item = self._items.get(key)
            if item is None:
                return None
            state = NEEDS_REAPPLYING if item.is_standing else OPEN
            item.set_by_person = False     # they reopened it; a run may now close it again
            item.record(state=state, note=note, by="person", at=at)
            self._save()
            return item


# ---------------------------------------------------------------------------------------------
# Recording a whole run's account, and the guidance bridge
# ---------------------------------------------------------------------------------------------

def ask_id(item_id: str, position: int) -> str:
    """The id of the nth ask inside one thing a person wrote.

    People do not write one ask per note. The live example this exists for, verbatim: "Fix that
    that add another column 'Proposed solution' and another 'Status' ... From now on I want as part
    of your report what you're doing on 1. limitations gathering and solutioning and 2. case
    gathering and preparation." That is a one-time request and a standing rule in one paragraph,
    and a record that can only hold one verdict for the pair has to lose one of them: mark it done
    and the standing rule stops existing, mark it standing and the column never gets built.

    So the first ask keeps the source's own id (the common case, unchanged) and each further ask is
    suffixed. They share their origin text and can be told apart in the ledger.
    """
    return item_id if position <= 1 else f"{item_id}#{position}"


def record_run_account(ledger: FeedbackLedger, *, card_id: str,
                       offered: Sequence[Tuple[str, str, str]],
                       dispositions: Dict[str, Any],
                       at: Optional[datetime] = None,
                       guidance_writer: Optional[Callable[..., Optional[str]]] = None,
                       run_id: str = "",
                       ) -> List[FeedbackItem]:
    """Fold one run's receipt into the ledger, and push any new standing rule into guidance.

    ``offered`` is ``[(ref, source, item_id), ...]`` as the run was shown them; ``dispositions`` is
    ``{ref: [(disposition, note), ...]}`` as the run wrote them (``parse_dispositions``), one entry
    per ask it is answering for on that line. Refs the run did not answer for are simply absent,
    which is correct: no account means no change.

    A line carrying more than one disposition splits the thing they wrote into that many asks (see
    ``ask_id``), each tracked on its own from then on.

    ``guidance_writer`` is called for each item that has just become a standing rule and has no
    card yet, as ``writer(text=..., item=...)``, and may return a card id to record. Duck-typed and
    optional, because how a deployment stores standing rules is a deployment's business: a file
    card manager, a hosted rules table, or nothing at all.
    """
    moved: List[FeedbackItem] = []
    for ref, source, item_id in offered:
        if not item_id:
            continue
        declared = _as_pairs(dispositions.get(ref))
        parent = ledger.get(card_id, source, item_id)
        for position, (disposition, note) in enumerate(declared, 1):
            if not disposition:
                continue
            this_id = ask_id(item_id, position)
            if position > 1 and ledger.get(card_id, source, this_id) is None and parent is not None:
                # A second ask inside the same paragraph: it inherits the origin, because their
                # words are the ask and both halves came out of the same sentence.
                ledger.observe(card_id=card_id, source=source, item_id=this_id,
                               text=parent.text, author=parent.author, location=parent.location,
                               url=parent.url, occurred_at=_parse(parent.occurred_at), at=at)
            item = ledger.apply_disposition(card_id=card_id, source=source, item_id=this_id,
                                            disposition=disposition, note=note, at=at,
                                            run_id=run_id)
            if item is None:
                continue
            moved.append(item)
            if item.is_standing and not item.guidance_card_id and guidance_writer is not None:
                # A standing rule's card carries the run's own words for WHICH half of the note is
                # the rule, when it split one, so the card is the instruction and not the paragraph
                # it arrived in.
                if len(declared) > 1:
                    # A MIXED note (a fix to make now AND a rule from now on). The card may only
                    # carry the run's own words for the standing half: carding the paragraph would
                    # put "add a Status column" into every future run's guidance as though it were
                    # policy, which is exactly the one-off-as-rule failure this is supposed to
                    # prevent. With no focused words there is nothing safe to card, so the item is
                    # still tracked as standing and simply gets no card this time. Their original
                    # paragraph is untouched on ``item.text`` either way.
                    rule_text = note
                    if not str(rule_text or "").strip():
                        log.info("feedback ledger: %s is standing inside a mixed note with no "
                                 "focused words; not carding the whole paragraph", item.key)
                        continue
                else:
                    rule_text = item.text
                try:
                    written = guidance_writer(text=rule_text, item=item)
                except Exception as e:  # noqa: BLE001 -- guidance never breaks a run
                    log.warning("feedback ledger: no guidance written for %s (%s)", item.key, e)
                    written = None
                if written:
                    item.guidance_card_id = str(written)
                    ledger._save()  # noqa: SLF001 -- same module; one write, not a second API
    return moved


def _as_pairs(value: Any) -> List[Tuple[str, str]]:
    """``[(disposition, note), ...]`` from whatever shape a caller passed for one ref."""
    if not value:
        return []
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return [(str(value[0]), str(value[1]))]
    if isinstance(value, str):
        return [(value, "")]
    out: List[Tuple[str, str]] = []
    for entry in value:
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            out.append((str(entry[0]), str(entry[1])))
        elif isinstance(entry, str):
            out.append((entry, ""))
    return out


def guidance_writer_for(manager: Any, *, tags: Sequence[str] = ()) -> Callable[..., Optional[str]]:
    """A ``guidance_writer`` backed by a ``GuidanceCardManager``-shaped object.

    The card is the person's own words, not a paraphrase. A standing instruction is already the
    clearest statement of itself, and a model rewriting it is one more place for it to drift from
    what they actually asked for.
    """
    def _write(*, text: str, item: FeedbackItem) -> Optional[str]:
        save = getattr(manager, "save_card", None) or getattr(manager, "create_card", None)
        if not callable(save) or not str(text or "").strip():
            return None
        card_id = f"standing_{item.source}_{item.item_id}".replace(":", "_").replace("/", "_")
        where = item.location or item.card_id
        body = (f"# {item.author or 'The person'} asked for this, and it stands\n\n"
                f"{text.strip()}\n\n"
                f"Asked on {item.occurred_at[:10] or 'an unrecorded date'}"
                f"{f', on {where}' if where else ''}. This is a standing instruction, not a "
                f"one-off: it applies to every piece of work it bears on, not only the one it was "
                f"written about.\n")
        try:
            save(card_id=card_id, title=f"Standing: {_first_words(text)}", body=body,
                 description=f"When working on {where}" if where else "",
                 tags=list(tags) or [f"scope:card:{item.card_id}"])
        except TypeError:
            # An older/other manager shape: positional id and body is the common denominator.
            save(card_id, body)
        return card_id
    return _write


def _first_words(text: str, words: int = 8) -> str:
    parts = " ".join(str(text or "").split())
    chunk = " ".join(parts.split(" ")[:words])
    return chunk if len(chunk) >= len(parts) else chunk + "..."


def ledger_path_for(configured: Optional[str] = None,
                    state_path: Optional[str] = None) -> Optional[str]:
    """Where this lane's ledger lives: what it configured, else beside its state file.

    Same derivation as ``context_updates.watermark_path_for``, and for the same reason: without a
    path the record lives for one process, and a record of what is still owed that forgets on
    restart is worse than none, because it reads as authoritative.
    """
    if configured:
        return configured
    if not state_path:
        return None
    p = Path(state_path)
    return str(p.with_name(p.stem + "_feedback.json"))


def build_ledger(cfg: Any = None, *, state_path: Optional[str] = None,
                 read_only: bool = False,
                 requires_acceptance: Optional[bool] = None) -> Optional[FeedbackLedger]:
    """The ledger a consumer's config asks for, or None when it asked for none.

    Duck-typed on the config object exactly as ``build_update_engine`` is, so a consumer with its
    own config, or a test with a stub, builds one without this module knowing what a
    ``RunnerConfig`` is.
    """
    if cfg is not None and not getattr(cfg, "feedback_ledger", True):
        return None
    path = ledger_path_for(getattr(cfg, "feedback_ledger_path", None), state_path)
    accept_gate = (getattr(cfg, "feedback_requires_acceptance", False)
                   if requires_acceptance is None else requires_acceptance)
    return FeedbackLedger(path, read_only=read_only, requires_acceptance=bool(accept_gate))


def open_items_block(items: Sequence[FeedbackItem], *, heading: str = "") -> str:
    """The standing-and-unfinished block a run is shown: what is still owed, and how far it got.

    Separate from the context-updates block on purpose. That block is news ("this arrived since you
    last looked"); this is the backlog ("this was asked for and is not finished"), and a person's
    half-applied instruction from three weeks ago belongs in the second, never the first.
    """
    rows = [i for i in items if i.is_open]
    if not rows:
        return ""
    head = heading or ("STILL OWED. These were asked for and are not finished. They are not new, "
                       "so nothing here arrived today: judge which bear on the work in front of "
                       "you, carry those further, and account for them in your receipt like "
                       "anything else.")
    lines = [head, ""]
    for item in rows:
        when = (item.occurred_at or item.first_seen_at)[:10] or "undated"
        who = item.author or "the person"
        lines.append(f"- [{when}] {who}, {item.status_line()}")
        lines.append(f"    {' '.join(str(item.text or '').split())[:400]}")
    return "\n".join(lines)
