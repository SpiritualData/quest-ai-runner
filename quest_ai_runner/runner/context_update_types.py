"""Context update data types: constants, ``ContextUpdate``, ``SourceGap`` and ``SourceReport``.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Sequence, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone


log = logging.getLogger("quest-ai-runner.context_updates")


# How far back a source looks when nothing has ever been recorded for this card. On a first run,
# everything recent IS new; without a floor, a first run either sees nothing (no watermark) or the
# entire history (no bound), and both are wrong.
FIRST_LOOK_DAYS = 14


# Sources every card gets whether or not it asked. These are not per-card interests, they are the
# channels a person uses to talk to the assistant AT ALL, so making any of them opt-in means a
# deployment can be blind to a direct reply and not know it.
#
# ``quest_notes`` is on this list because of a live failure (2026-09-10). A run answered a person's
# emailed reply -- which lands as a note on the quest -- and its receipt listed only the reflection
# and the captures, because notes were opt-in and the backend rejects the very field a quest would
# declare them in (422 ``extra_forbidden``). So the one channel the person had actually just used
# was the one channel the engine never looked at. A reply channel that a deployment has to remember
# to switch on is a reply channel that is off.
#
# ``goal_updates`` is on this list for the same shape of reason, minus the opt-in step ever having
# existed: before ``GoalUpdatesSource``, nothing in this library read a goal update at all, on any
# quest, so an autopilot pass never saw the one place a person writes what they did or found on a
# single goal. Making it opt-in would just reproduce the ``quest_notes`` failure under a new name.
# It is safe to default on precisely because it is card-scoped by construction (an update is
# written ON a goal that belongs to THIS quest), so turning it on can never leak another card's
# material the way a user-scoped channel could.
#
# ``quest_events`` is on this list for the identical reason as ``goal_updates``, one level up: a
# quest-lifecycle change (the outcome rewritten, a milestone completed, the deadline moved) is
# recorded against THIS quest's own id by construction (see ``QuestEventsSource``'s ``collect``,
# which asks the backend for exactly this quest's rows), so defaulting it on can never surface
# another card's activity. It is also the one channel that lets an autopilot pass find out a
# person acted WITHOUT writing anything down anywhere -- completing a milestone or editing a
# field leaves no note and no goal update, so a pass that only read those two channels would see
# nothing at all and re-propose work the person had already done. Making it opt-in would
# reproduce the exact ``quest_notes`` failure above under a new name: a signal that exists but
# that a deployment has to remember to switch on is a signal that is effectively off.
DEFAULT_ALWAYS: Sequence[str] = ("reflections", "insights", "quest_notes", "goal_updates",
                                "quest_events", "decisions")


# Looking for where the previous AUTOPILOT RUN's own output comes from? It is NOT a source here.
# ``runner/autopilot.py``'s ``compose_batch_text`` carries it as its own ``last_run`` parameter,
# deliberately outside this registry: a source here is watermarked (delivered once, until
# ``mark_seen()`` moves past it) because it answers "what has arrived since an assistant last
# looked", and the previous run's output has to be in front of EVERY pass, not delivered once and
# then withheld from the very next one. It is also the assistant's OWN prior output, not material
# that arrived FROM a person, which is what every source in this registry models (see
# ``QuestNotesSource`` above). See ``compose_batch_text``'s docstring for the full reasoning.
#
# ``QuestEventsSource`` below is the opposite call, on purpose, not an oversight of this rule.
# "What arrived since I last looked" is EXACTLY the question a quest's own lifecycle events
# answer -- a milestone completed, an outcome rewritten -- so watermarking them is the correct
# semantics here, the same way it is for ``QuestNotesSource`` and ``GoalUpdatesSource``, not the
# mistake it would be for the previous run's own output above. And the material is not this
# assistant's own prior output either: it is filtered to ``actor == "app"`` precisely so it only
# ever carries something a PERSON did, which is what belongs in this registry in the first place.

# "Open until answered" needs a floor and a cap, or it stops being a question and becomes a
# backlog. A note nobody answered a year ago is not something the person is waiting on today, and
# carrying it into every brief forever trains the reader to skim past all of them -- which is the
# same failure the first-look bound exists to prevent at the other end.
OPEN_ITEM_MAX_AGE_DAYS = 90


# ``feedback_ledger.KIND_UNKNOWN``, restated rather than imported: that module imports nothing from
# here and this one must not import it at module level either (the ledger is optional, and a
# deployment that does not keep one should not pay an import for it). Held equal by a test.
KIND_UNKNOWN_SENTINEL = "unknown"


# How many still-open items ONE source may carry. Newest first, so a burst of notes surfaces the
# recent ones and lets the older tail go.
MAX_OPEN_PER_SOURCE = 6


# Hard cap on updates carried in one bundle, newest first. The brief already holds the goals, the
# plan of record and the instruction; a comment spree must not push the actual work out of the
# model's attention.
MAX_UPDATES = 20


# Per-item body cap in the composed block, for a body this module COMPOSED itself -- a rendered
# habit log, a file-change line, a summary. Shortening one of those costs a run nothing.
MAX_BODY_CHARS = 800


# The cap for a body that is a PERSON'S OWN WORDS, which is a different thing entirely. At 800 a
# 1,600-character reply asking for three changes reached a run cut mid-sentence, and the run told
# its author "your message cut off there, tell me what part of it still needs a response": it read
# our shortening as their typing, handed the failure back to them, and never saw the two asks that
# sat past the cut. What a cap is FOR is one burst of material not swallowing the brief; a person
# writing a long instruction is not the failure case. 12,000 matches the cap a mailed note already
# passes through on the way in, so a note that reached the quest whole reaches the run whole.
MAX_PERSON_BODY_CHARS = 12000


# ...and a ceiling across ONE block, spent newest-first, so a pile of long notes still cannot push
# the actual work out of the model's attention. Anything past the budget falls back to the short
# cap -- with the marker below, which says the words are elsewhere rather than missing.
PERSON_BODY_BUDGET = 24000


# How long the engine's user-scoped reads (reflections, captures) stay cached. Long enough that one
# pass over every quest, or one executor task's context view, reads each once; short enough that a
# task at two in the afternoon does not see the captures as they stood at six in the morning. The
# poller builds ONE engine for its whole life, so without this the cache never refreshed at all.
CACHE_TTL_SECONDS = 120


# The block delimiters (defined in core.managed_blocks, re-exported here).
from ..core.managed_blocks import BLOCK_END, BLOCK_START  # noqa: E402,F401


# The heading a run writes its usage lines under, and the line shape it uses. Both are stated in
# the gate text below, so the run is told exactly what is parsed.
USAGE_HEADING = "Context used:"


_REF_RE = re.compile(r"^\s*[-*]?\s*\[(U\d+)\]\s*(.*)$")


# A sort key for an item with no timestamp: it goes last among the person's words, so a dated
# note is never shortened in favour of an undated one.
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: Any, limit: int = MAX_BODY_CHARS) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + " [...truncated]"


def _preview(text: Any, limit: int) -> str:
    """The first words of something, for an index line.

    Ends in an ellipsis, not the truncation marker: an index line is openly a preview, while
    "[...truncated]" sitting after someone's half-sentence reads as the sentence ending -- the
    exact confusion the body cap below exists not to cause.
    """
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit].rstrip() + "\u2026"


def _clip_body(text: Any, limit: int, *, verbatim: bool = False) -> str:
    """One item's body for the block, shortened only if it has to be.

    A bare "[...truncated]" is enough on a body this module composed. On a person's own words it
    is not: the run cannot tell OUR cut from the end of what they wrote, and the one time it
    matters -- a reply with instructions running past the cap -- it will answer the half it can
    see and tell the person the rest of their message went missing. So the marker on their words
    says who shortened it, how much is not shown, and that the whole thing is readable at source.
    """
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    kept = s[:limit].rstrip()
    if not verbatim:
        return kept + " [...truncated]"
    return kept + (f" [...{len(s) - len(kept)} more characters of their own words are not shown "
                   "here. THIS LIST shortened them; their message did not end here and nothing "
                   "they wrote was lost. Read the item in full at its source before answering "
                   "it, and never tell them their message cut off.]")


def _as_utc(value: Any) -> Optional[datetime]:
    """A timestamp in any shape this library sees, as aware UTC, or None."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SourceGap(Exception):
    """A source cannot look, and says so plainly: a gap in the bundle, not a failure.

    Raised from ``collect`` when the thing the card asked for is not available on this deployment
    (no client wired, a space or database that is not on the allowlist, a spec that names nothing).
    The engine reports it as the source's ``error`` with the message as written and does NOT log a
    warning, and -- because the report is not ``ok`` -- the watermark does not advance, so nothing
    is consumed by a read that never happened. Any other exception is reported with its type name
    and logged as a failure.
    """


# ---------------------------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------------------------

@dataclass
class ContextUpdate:
    """One thing that arrived since an assistant last looked at this card.

    ``ref`` is assigned by the bundle, not by the source: it has to be unique and stable ACROSS
    sources within one delivery, since it is the handle the run answers with.

    ``how_to_respond`` is what separates this from a feed. A note can be answered with a note, a
    document comment with a reply on that comment, a capture by ticking acted-on. A run that is
    shown material with no channel back can only mention it, and mentioning it is what makes a
    person stop writing them.
    """
    source: str = ""                 # the registered source name ("insights", "drive_comments")
    kind: str = ""                   # what sort of thing ("comment", "note", "capture")
    item_id: str = ""                # the source's own id, for dedup and for replying
    title: str = ""                  # a short label: who/where, one line
    body: str = ""                   # the content itself, in their words
    author: str = ""
    occurred_at: Optional[datetime] = None
    location: str = ""               # the document, quest, or collection it came from
    url: str = ""
    how_to_respond: str = ""         # the channel back, in one imperative phrase
    needs_response: bool = False     # a question left open, as opposed to something to know
    # The body is the person's OWN WORDS rather than something this module composed, so it is
    # shown whole up to ``MAX_PERSON_BODY_CHARS`` instead of the short per-item cap.
    verbatim: bool = False
    ref: str = ""                    # "U1" -- assigned at bundle time
    # A named position a composer already reserves for this channel, when it has one.
    # The reflection and the insights blocks were composed into a brief long before this engine
    # existed, each with framing prose that took work to get right ("let that steer which of the
    # above matters most", "the tags are how they label their own thinking"). Routing them through
    # the engine must not cost that. A slotted update is collected here, gets a ref here, and is
    # counted in the receipt here, but it renders through the composer's existing slot instead of
    # in the general block. Everything with no slot goes in the general block.
    slot: str = ""
    # The first words of what the person wrote, for the manifest line. A receipt row that says only
    # "note · Dissertation" does not tell the person WHICH note reached the run; the row has to
    # carry enough of their own words to be recognised without opening the task.
    excerpt: str = ""
    # One condensed line for an artifact that holds one line of context (the next-steps note).
    summary: str = ""
    # (header, footer) around the rows of a slot, set by the source that fills it. The captures
    # block carries a closing instruction that took work to get right; rendering the captures one
    # row each must not cost it. ``slot_text`` uses the first frame it finds among the slot's rows.
    slot_frame: Optional[Tuple[str, str]] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def manifest_line(self) -> str:
        """The one-line form used both in the offered block and in the receipt.

        Fixed field order, separated by a middle dot, so a person can scan a column of them and so
        ``parse_manifest`` can read them back: ref, date, kind, who/where, their first words, and
        whether it is waiting on an answer.
        """
        when = self.occurred_at.strftime("%Y-%m-%d") if self.occurred_at else "undated"
        where = self.location or self.author or self.source
        said = f' · "{_preview(self.excerpt, 70)}"' if self.excerpt else ""
        flag = " · needs an answer" if self.needs_response else ""
        return (f"[{self.ref}] {when} · {self.kind or self.source} · "
                f"{_preview(where, 60)}{said}{flag}")

    def body_limit(self) -> int:
        """This item's own body cap, before any block-level budget narrows it."""
        return MAX_PERSON_BODY_CHARS if self.verbatim else MAX_BODY_CHARS

    def as_text(self, *, body_limit: Optional[int] = None) -> str:
        """The full item for the prompt: the manifest line, then what was actually said.

        ``body_limit`` is the block's say over this item (see ``ContextUpdates.body_limits``); on
        its own the item uses its own cap.
        """
        lines = [self.manifest_line()]
        if self.title:
            lines.append(f"    {self.title}")
        if self.body:
            limit = self.body_limit() if body_limit is None else body_limit
            lines.append(f"    {_clip_body(self.body, limit, verbatim=self.verbatim)}")
        if self.how_to_respond:
            lines.append(f"    to respond: {self.how_to_respond}")
        return "\n".join(lines)


@dataclass
class SourceReport:
    """What one source did on one collection pass.

    "Checked, found nothing" and "did not look" are different pieces of information and a run that
    cannot tell them apart will hedge about both. Kept for every source, including the ones that
    produced no updates, so the bundle can say which.
    """
    source: str = ""
    spec: Dict[str, Any] = field(default_factory=dict)
    since: Optional[datetime] = None
    found: int = 0
    set_aside: int = 0          # collected, then judged not to bear on this card
    error: str = ""
    # How many items the source LOOKED at before its own filtering, and one line on why fewer were
    # offered. Without these, "0 found" is indistinguishable from "two questions, both already
    # answered in the document" -- and the person reading the first one concludes the channel is
    # broken. It cost a live round trip: a quest watching two Drive routes reported nothing new for
    # days while both of the person's comments sat there, answered, exactly as designed.
    considered: int = 0
    explanation: str = ""
    # What the source asked to remember for its NEXT look (``CollectRequest.remember``), keyed by
    # the source's own choice of key. Stored by ``mark_seen`` beside the watermark, for the same
    # reason: it only counts once a run has actually been handed what produced it.
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def one_line(self) -> str:
        """What this source did, in the words a person would use."""
        if self.error:
            return f"{self.source} (could not read: {self.error})"
        if self.found and self.set_aside:
            return f"{self.source} ({self.found}, {self.set_aside} not about this work)"
        if self.found:
            return f"{self.source} ({self.found})"
        if self.explanation:
            return f"{self.source} ({self.explanation})"
        return f"{self.source} (nothing new)"
