"""The ``ContextUpdates`` bundle one collection hands a run, and how it renders and marks things seen.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .context_update_types import (
    BLOCK_END,
    BLOCK_START,
    ContextUpdate,
    MAX_BODY_CHARS,
    MAX_PERSON_BODY_CHARS,
    PERSON_BODY_BUDGET,
    SourceReport,
    _OLDEST,
    _utcnow,
    log,
)
from .context_update_receipts import _OFFER_PREAMBLE, usage_receipt_gate
from .context_update_state import Watermarks


@dataclass
class ContextUpdates:
    """Everything that arrived for one card, plus the record of what was checked to find it."""
    card_id: str = ""
    card_label: str = ""
    # What KIND of thing card_id names ("quest", ...). Relaying (see relay_admitted) only ever
    # posts a QUEST note, so this is what tells it whether card_id is even the right kind of id to
    # post one against; a caller collecting against some other kind of card is unaffected.
    card_kind: str = "quest"
    updates: List[ContextUpdate] = field(default_factory=list)
    reports: List[SourceReport] = field(default_factory=list)
    collected_at: datetime = field(default_factory=_utcnow)
    # Set by the engine so ``mark_seen`` can advance exactly what was delivered.
    _watermarks: Optional["Watermarks"] = None
    # The record of what became of things (``runner.feedback_ledger``), when one is kept. Written
    # at the same moment as the watermarks and for the same reason: delivery is the only honest
    # moment to say an item reached a run.
    _ledger: Any = None
    # The card this bundle was collected for, so an owed item can be rendered with the right
    # reply channel without another fetch.
    _card: Dict[str, Any] = field(default_factory=dict)
    # The source names whose items are asks (see ``_BaseSource.tracks_asks``). Only these are
    # entered in the ledger, so a person is never asked to answer their own reflection.
    _ask_sources: frozenset = frozenset()
    # The Quest API client, set by the engine, that relay_admitted posts a note through. None on
    # any engine built with no client (an offline test, a stub) -- relaying is then simply skipped,
    # the same "unconfigured means this contributes nothing" degrade every optional channel has.
    relay_client: Any = None
    # Where relay_admitted records "already posted", so a restart or a second collection of the
    # same item never posts it twice. See RelayedItems.
    relay_log: Any = None
    # The source names whose admitted items get posted onto the quest (see
    # ``_BaseSource.relay_to_quest``). Only these ever reach relay_admitted's loop.
    relay_sources: frozenset = frozenset()

    def __bool__(self) -> bool:
        return bool(self.updates)

    def has_any(self) -> bool:
        """Whether anything arrived. Named to match ``ReflectionContext``/``InsightsContext``, so a
        caller holding any of the three asks the same question the same way."""
        return bool(self.updates)

    def needing_response(self) -> List[ContextUpdate]:
        """Just the items someone is actually waiting on an answer to."""
        return [u for u in self.updates if u.needs_response]

    def by_source(self, source: str) -> List[ContextUpdate]:
        return [u for u in self.updates if u.source == source]

    def refs(self) -> List[str]:
        """Every ref offered this run, in order, slotted ones included."""
        return [u.ref for u in self.updates if u.ref]

    def remember_offer(self, task_id: str) -> None:
        """Hand this bundle's ref-to-item map to the ledger for the task that will carry it.

        A brief composed now is run later by an executor that holds only the task; this is what
        lets that run's receipt move the items it accounts for (``FeedbackLedger.remember_offer``).
        Never raises.
        """
        if self._ledger is None or not task_id:
            return
        try:
            self._ledger.remember_offer(task_id, self.card_id, self.offered_keys())
        except Exception as e:  # noqa: BLE001 -- bookkeeping never costs a pass its work
            log.warning("context updates: could not remember the offer for %s (%s)", task_id, e)

    def offered_keys(self) -> List[Tuple[str, str, str]]:
        """``[(ref, source, item_id), ...]`` for everything offered that a ledger can track.

        The handle between the receipt a run writes and the record of what became of each thing:
        the run answers by ref, and this says which item each ref was.
        """
        return [(u.ref, u.source, u.item_id) for u in self.updates if u.ref and u.item_id]

    # --- what the run is shown --------------------------------------------------------------

    def slot_text(self, slot: str) -> str:
        """The text for one composer slot (``"reflection"``, ``"insights"``), ref-tagged, or "".

        The ref prefix is what keeps a slotted channel inside the receipt: the run is asked to
        account for every ref it was shown, and a reflection rendered with no ref could only ever
        come back as "not used" because the run had no handle to name it by.
        """
        rows = [u for u in self.updates if u.slot == slot and u.body]
        if not rows:
            return ""
        body = "\n\n".join(f"[{u.ref}] {u.body}" if u.ref else u.body for u in rows)
        header, footer = next((u.slot_frame for u in rows if u.slot_frame), ("", ""))
        return "\n".join(part for part in (header, body, footer) if part)

    def slot_summary(self, slot: str) -> str:
        """The one-line summary of a slotted channel, for an artifact that holds one line.

        The newest row's own summary, plus how many more rows the slot holds.
        """
        rows = [u for u in self.updates if u.slot == slot]
        for u in rows:
            if u.summary:
                more = f" (+{len(rows) - 1} more)" if len(rows) > 1 else ""
                return u.summary + more
        return ""

    def as_prompt_block(self, *, ask_for_receipt: bool = True,
                        exclude_slots: Sequence[str] = ()) -> str:
        """The general block a brief or a context view carries, or "" when nothing arrived.

        Framed as material to JUDGE, not a list to work through, for the same reason the insights
        block is: a person's comment on chapter two is not automatically this run's assignment, and
        a run told to action everything it is shown will do exactly that.

        ``exclude_slots`` leaves out the channels the caller renders through its own slots (see
        ``ContextUpdate.slot``). They still appear in the receipt gate's ref list, because the run
        was shown them -- somewhere else in the same brief.
        """
        if not self.updates:
            return ""
        skip = set(exclude_slots or ())
        detailed = [u for u in self.updates if u.slot not in skip]
        lines: List[str] = [BLOCK_START, _OFFER_PREAMBLE, ""]
        # The INDEX first: one line for every ref offered this run, including the ones rendered
        # elsewhere in the brief. Two things depend on it. The run needs to see the full list to
        # account for it, and ``parse_manifest`` reads this block to rebuild the receipt from a
        # task's own text -- so a ref missing here is a ref the receipt can never report on.
        for u in self.updates:
            suffix = "" if u.slot not in skip else "  (rendered in its own section of this brief)"
            lines.append(u.manifest_line() + suffix)
        lines.append("")
        limits = self.body_limits(detailed)
        for u in detailed:
            lines.append(u.as_text(body_limit=limits.get(id(u))))
            lines.append("")
        lines.append(BLOCK_END)
        # The receipt gate sits OUTSIDE the delimiters on purpose: it names example ref lines, and
        # ``parse_manifest`` reads every ref line BETWEEN the delimiters. Inside, the examples
        # would be recovered as if they were offered updates, and the receipt would list rows that
        # never existed.
        if ask_for_receipt and self.updates:
            if lines:
                lines.append("")
            lines.append(usage_receipt_gate(self.refs()))
        return "\n".join(lines)

    @staticmethod
    def body_limits(updates: Sequence["ContextUpdate"]) -> Dict[int, int]:
        """``{id(update): body cap}`` for one block, spending ``PERSON_BODY_BUDGET`` newest-first.

        Newest-first because the newest thing a person wrote is the one still waiting on an
        answer; an older note that already had a reply is the one that can afford to be short.
        Every item keeps at least ``MAX_BODY_CHARS`` however spent the budget is, so a long day
        shortens the tail rather than blanking it.
        """
        limits: Dict[int, int] = {}
        spent = 0
        verbatim = sorted((u for u in updates if u.verbatim and u.body),
                          key=lambda u: u.occurred_at or _OLDEST, reverse=True)
        for u in verbatim:
            room = max(MAX_BODY_CHARS, min(MAX_PERSON_BODY_CHARS, PERSON_BODY_BUDGET - spent))
            limits[id(u)] = room
            spent += min(len(" ".join(str(u.body).split())), room)
        return limits

    def checked_line(self) -> str:
        """One line naming what was checked and what it held, for a run that got nothing.

        Worth emitting even when the bundle is empty: "I looked at your comments and captures,
        there was nothing new" is a real answer, and its absence is what makes a person ask
        whether the assistant is reading them at all.
        """
        if not self.reports:
            return ""
        bits = [r.one_line() for r in self.reports]
        return "Context sources checked this run: " + ", ".join(bits) + "."

    # --- the receipt ------------------------------------------------------------------------

    def manifest(self) -> List[str]:
        return [u.manifest_line() for u in self.updates]

    # --- bookkeeping --------------------------------------------------------------------------

    def as_dict(self) -> Dict[str, Any]:
        """The whole bundle as plain JSON-able data, for a caller that is not a prompt.

        Here rather than in whoever is printing it: the field list is this module's, and a consumer
        rebuilding it by hand drifts from it the first time a field is added. Timestamps are
        ISO-8601 strings; the watermark store is deliberately absent (it is wiring, not content).
        """
        return {
            "card_id": self.card_id,
            "card_label": self.card_label,
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "updates": [
                {
                    "ref": u.ref, "source": u.source, "kind": u.kind, "item_id": u.item_id,
                    "title": u.title, "body": u.body, "author": u.author,
                    "occurred_at": u.occurred_at.isoformat() if u.occurred_at else None,
                    "location": u.location, "url": u.url,
                    "how_to_respond": u.how_to_respond, "needs_response": u.needs_response,
                    "slot": u.slot, "excerpt": u.excerpt, "summary": u.summary,
                    "manifest_line": u.manifest_line(),
                }
                for u in self.updates
            ],
            "reports": [
                {
                    "source": r.source, "spec": r.spec, "found": r.found,
                    "set_aside": getattr(r, "set_aside", 0),
                    "considered": r.considered,
                    "explanation": r.explanation,
                    "since": r.since.isoformat() if r.since else None,
                    "error": r.error,
                }
                for r in self.reports
            ],
        }

    def mark_seen(self, *, at: Optional[datetime] = None) -> None:
        """Advance each checked source's watermark for this card.

        Called by the caller AFTER the updates have actually been handed to a run, never at
        collection time: a pass that collects and then fails to compose anything must not have
        consumed the person's comment on the way past.

        Only sources that reported WITHOUT error advance. A source that could not be read has not
        been seen, and pretending otherwise turns one API blip into permanently lost context.
        """
        if not self.card_id:
            return
        stamp = at or self.collected_at
        if self._watermarks is not None:
            for r in self.reports:
                if r.ok:
                    self._watermarks.set(self.card_id, r.source, stamp)
                    for key, snapshot in (r.snapshots or {}).items():
                        self._watermarks.set_snapshot(self.card_id, r.source, key, snapshot)
        # The same moment, the same reason: this is when an item was actually put in front of a
        # run, which is what "offered" means. It records existence and nothing else -- being shown
        # something is not acting on it, and the ledger is careful about the difference.
        if self._ledger is not None:
            for u in self.updates:
                if not u.item_id or u.source not in self._ask_sources:
                    continue
                try:
                    self._ledger.observe(
                        card_id=self.card_id, source=u.source, item_id=u.item_id,
                        text=u.body or u.excerpt, author=u.author, location=u.location,
                        url=u.url, occurred_at=u.occurred_at, at=stamp)
                except Exception as e:  # noqa: BLE001 -- bookkeeping never breaks a delivery
                    log.warning("feedback ledger: could not record %s (%s)", u.item_id, e)
        self.relay_admitted()

    def relay_admitted(self) -> None:
        """Post each admitted item from a ``relay_to_quest`` source onto the quest as a note.

        Called from ``mark_seen`` -- the same "delivery is the only honest moment" reasoning as
        the ledger's ``observe`` call just above, and for a stronger reason here: this is a WRITE
        with a real person's own words and address on it, so it must happen exactly once a run has
        actually been handed the material, never at collection time (which is what makes
        ``quest-ai-runner context <quest_id>`` -- built on a read-only engine that never calls
        ``mark_seen`` at all -- a genuine read: it cannot cause this side effect either).

        Posts through ``QuestClient.add_quest_note(..., relayed_author_email=...)``, which is the
        one write path that lets an API-key caller attribute a note to a real person's address
        rather than to the AI (see that method's own docstring and ``docs/feedback-ledger.md`` on
        why the account's own asks record -- not this library's local feedback ledger -- is what
        the person's name has to reach). Idempotent by (card_id, source, item_id) via
        ``relay_log``: a restart, or a "still owed" re-offer of the same mail item on a later pass,
        must never turn into a second note under the same person's name.

        Best-effort, like every write this engine treats as optional plumbing rather than the
        thing itself: a relay failure is logged and the item is simply tried again next time
        (``relay_log`` is only updated on success), and it never raises into a caller that only
        expected watermarks and a ledger entry to move.
        """
        if (self.relay_client is None or not self.relay_sources or not self.card_id
                or self.card_kind != "quest"):
            return
        add_note = getattr(self.relay_client, "add_quest_note", None)
        if not callable(add_note):
            return
        for u in self.updates:
            if u.source not in self.relay_sources or not u.item_id:
                continue
            author_email = (u.author or "").strip().lower()
            text = (u.body or u.excerpt or "").strip()
            if not author_email or not text:
                continue
            if self.relay_log is not None and self.relay_log.already_relayed(
                    self.card_id, u.source, u.item_id):
                continue
            # A plain length cut with a bare marker, NOT ``_clip_body``'s verbatim wording: that
            # marker is written for a prompt a run reads ("read the item in full at its source"),
            # and this text is going into a stored NOTE, not a prompt block. The same marker
            # quest-backend's own inbound-reply capture writes when IT truncates a long reply, so
            # a note that hit this cap reads identically whichever path relayed it.
            note_text = text if len(text) <= MAX_PERSON_BODY_CHARS else (
                text[:MAX_PERSON_BODY_CHARS].rstrip() + "\n\n[truncated]")
            try:
                add_note(self.card_id, note_text, relayed_author_email=author_email)
            except Exception as e:  # noqa: BLE001 -- a relay failure never breaks delivery
                log.warning("context updates: could not relay %s:%s (%s)",
                           u.source, u.item_id, e)
                continue
            if self.relay_log is not None:
                self.relay_log.mark_relayed(self.card_id, u.source, u.item_id)
