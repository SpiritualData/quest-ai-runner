"""context_updates -- ONE place that answers "what has changed since an assistant last looked?"

THE PROBLEM THIS EXISTS TO FIX. Every context channel in this library was built the same way and
wired by hand twice. ``runner.reflections`` reads what the person wrote; ``runner.insights`` reads
what they captured; ``executor._build_context_view`` reads the quest's notes and run history;
``AutopilotPass._reflections``/``._insights`` read two of those again, separately, with their own
caching and their own slot in ``compose_batch_text``. Each was a good module. Together they meant
that adding a THIRD channel (a comment on a document, a folder the work owns, a category of
capture that matters to one quest) was not "register a source", it was "write a module, write a
fetch method on two callers, write a render function, add a parameter to the composer, and
remember to tell the run to look at it". So people did the last part in the prompt instead: the
quest's standing instructions grew lines like "check the comments on the doc", "look at my
insights tagged X" -- a person hand-maintaining, in prose, the retrieval plan that the context
engine was supposed to own.

THE SHAPE HERE. A ``ContextSource`` answers one question: given a card and a watermark, what has
arrived since? An ``UpdateEngine`` holds the registry, resolves WHICH sources a given card uses
(from data the card carries, never from a hardcoded map), runs them, and returns a
``ContextUpdates`` bundle. Every caller -- the autopilot pass composing a brief, the executor
building a task's context view, an attended session -- asks the same engine the same question, so
a source added once is seen by all of them.

CUSTOM PER CARD, AS DATA. What a card watches is a list of specs the card itself carries:
``{"source": "insights", "categories": ["PhD"]}``, ``{"source": "drive_comments",
"folder_id": "1kEc..."}``, ``{"source": "quest_notes"}``. An assistant can write a spec (it is
JSON, and the vocabulary is discoverable via ``UpdateEngine.describe_sources()``); nothing in this
module hardcodes that any particular card watches any particular thing, and an unknown source name
degrades to a reported gap rather than an exception.

A TAG NEVER GATES DELIVERY. ``runner.insights`` refuses to match tags against quest names for a
good reason (hard rule #3): a fixed string rule silently loses every capture whose wording it did
not anticipate. So a ``categories`` spec here does not filter the captures. Every capture is its
own update with its own ref; a tag match only FLAGS one as waiting on an answer. The only thing
that sets a capture aside is the relevance judge, which is a model reading the card's subject
matter, and any failure of the judge keeps everything.

THE RECEIPT. Surfacing context is only half the loop: a person who leaves a comment or captures an
insight has no way to know whether the run that followed actually used it, and "I read your note"
buried in three paragraphs of result is not an answer. So every offered update carries a short
stable ref (``[U1]``), the prompt block asks the run to close with one line per ref saying in a few
words how it used it, and ``render_receipt`` turns the run's OWN answer into a fixed-format block
appended to the result. That is deliberately not a second model call grading the first: a summary
produced by another pass would be a guess about a run it did not do, would cost a call per run, and
would be exactly the afterthought this is meant not to be. The run that used the material is the
only thing that knows how it used it. When a run says nothing about a ref, the receipt says so
plainly rather than inventing a use.

Everything here is best-effort: a source that raises is reported as a failed source and the rest of
the bundle is delivered. A card with no specs, a client missing methods, an unreachable API and an
empty inbox all produce an EMPTY bundle, which every caller treats as "nothing new" rather than as
a failure.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

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
DEFAULT_ALWAYS: Sequence[str] = ("reflections", "insights", "quest_notes")

# "Open until answered" needs a floor and a cap, or it stops being a question and becomes a
# backlog. A note nobody answered a year ago is not something the person is waiting on today, and
# carrying it into every brief forever trains the reader to skim past all of them -- which is the
# same failure the first-look bound exists to prevent at the other end.
OPEN_ITEM_MAX_AGE_DAYS = 90
# How many still-open items ONE source may carry. Newest first, so a burst of notes surfaces the
# recent ones and lets the older tail go.
MAX_OPEN_PER_SOURCE = 6

# Hard cap on updates carried in one bundle, newest first. The brief already holds the goals, the
# plan of record and the instruction; a comment spree must not push the actual work out of the
# model's attention.
MAX_UPDATES = 20

# Per-item body cap in the composed block.
MAX_BODY_CHARS = 800

# How long the engine's user-scoped reads (reflections, captures) stay cached. Long enough that one
# pass over every quest, or one executor task's context view, reads each once; short enough that a
# task at two in the afternoon does not see the captures as they stood at six in the morning. The
# poller builds ONE engine for its whole life, so without this the cache never refreshed at all.
CACHE_TTL_SECONDS = 120

# The block delimiters. They are parsed back out of a composed task text (see ``parse_manifest``),
# which is what lets a consumer render the receipt from the task it just ran without threading a
# bundle object through the queue.
BLOCK_START = "=== CONTEXT UPDATES ==="
BLOCK_END = "=== END CONTEXT UPDATES ==="

# The heading a run writes its usage lines under, and the line shape it uses. Both are stated in
# the gate text below, so the run is told exactly what is parsed.
USAGE_HEADING = "Context used:"
_REF_RE = re.compile(r"^\s*[-*]?\s*\[(U\d+)\]\s*(.*)$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: Any, limit: int = MAX_BODY_CHARS) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + " [...truncated]"


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
        said = f' · "{_clip(self.excerpt, 70)}"' if self.excerpt else ""
        flag = " · needs an answer" if self.needs_response else ""
        return f"[{self.ref}] {when} · {self.kind or self.source} · {_clip(where, 60)}{said}{flag}"

    def as_text(self) -> str:
        """The full item for the prompt: the manifest line, then what was actually said."""
        lines = [self.manifest_line()]
        if self.title:
            lines.append(f"    {self.title}")
        if self.body:
            lines.append(f"    {_clip(self.body)}")
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

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class ContextUpdates:
    """Everything that arrived for one card, plus the record of what was checked to find it."""
    card_id: str = ""
    card_label: str = ""
    updates: List[ContextUpdate] = field(default_factory=list)
    reports: List[SourceReport] = field(default_factory=list)
    collected_at: datetime = field(default_factory=_utcnow)
    # Set by the engine so ``mark_seen`` can advance exactly what was delivered.
    _watermarks: Optional["Watermarks"] = None

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
        for u in detailed:
            lines.append(u.as_text())
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

    def checked_line(self) -> str:
        """One line naming what was checked and what it held, for a run that got nothing.

        Worth emitting even when the bundle is empty: "I looked at your comments and captures,
        there was nothing new" is a real answer, and its absence is what makes a person ask
        whether the assistant is reading them at all.
        """
        if not self.reports:
            return ""
        bits = []
        for r in self.reports:
            if r.error:
                bits.append(f"{r.source} (could not read: {r.error})")
            elif r.found and r.set_aside:
                bits.append(f"{r.source} ({r.found}, {r.set_aside} not about this work)")
            elif r.found:
                bits.append(f"{r.source} ({r.found})")
            else:
                bits.append(f"{r.source} (nothing new)")
        return "Context sources checked this run: " + ", ".join(bits) + "."

    # --- the receipt ------------------------------------------------------------------------

    def manifest(self) -> List[str]:
        return [u.manifest_line() for u in self.updates]

    # --- bookkeeping --------------------------------------------------------------------------

    def mark_seen(self, *, at: Optional[datetime] = None) -> None:
        """Advance each checked source's watermark for this card.

        Called by the caller AFTER the updates have actually been handed to a run, never at
        collection time: a pass that collects and then fails to compose anything must not have
        consumed the person's comment on the way past.

        Only sources that reported WITHOUT error advance. A source that could not be read has not
        been seen, and pretending otherwise turns one API blip into permanently lost context.
        """
        if self._watermarks is None or not self.card_id:
            return
        stamp = at or self.collected_at
        for r in self.reports:
            if r.ok:
                self._watermarks.set(self.card_id, r.source, stamp)


# ---------------------------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------------------------

_OFFER_PREAMBLE = (
    "Things the people you work with have said or done since you last looked at this work, in "
    "their own words. This is not a task list: judge which of these bear on what you are doing "
    "now, act on those, and leave the rest. Anything marked \"needs an answer\" is a question "
    "someone is waiting on -- answer it through the channel named under it, not only in your "
    "result, or they will never see the answer."
)

# The receipt protocol, stated to the run that will produce the work. Kept here rather than in
# ``core/context_doctrine`` because it is not a doctrine about how to think: it is the contract for
# one specific block, and it is meaningless without the refs that block carries.
_RECEIPT_RULES = (
    'Write what you ACTUALLY did with it ("cited in the method section", "answered in the doc", '
    '"contradicts the plan, flagged"), not what it was about. "not used" is a fine and common '
    'answer -- say it plainly rather than inventing a use, because the person reads these lines to '
    'find out whether what they wrote reached you, and a made-up one is worse than an honest no. '
    "This is your own account of your own run: nothing else writes it for you, and no second pass "
    "goes back and guesses."
)


def usage_receipt_gate(refs: Sequence[str] = ()) -> str:
    """The closing instruction, naming the exact refs this run was shown.

    Enumerating them rather than describing them is the point: a run given "one line per ref" and a
    generic example reliably answers for the ones it found interesting and drops the rest, and the
    dropped ones are precisely the material the person is checking on.
    """
    listed = [str(r) for r in (refs or []) if str(r)]
    if listed:
        example = "\n".join(
            f"  [{r}] " + ("<up to 5 words on how you used it, or: not used>" if i == 0 else "...")
            for i, r in enumerate(listed))
        which = f"one line for each of {', '.join(listed)}, in that order, none left out"
    else:
        example = "  [U1] <up to 5 words on how you used it>\n  [U2] not used"
        which = "one line per ref you were shown above, in order, none left out"
    return (f"BEFORE YOU FINISH, account for the context updates you were shown. End your result "
            f"with:\n\n{USAGE_HEADING}\n{example}\n\nThat is {which}. {_RECEIPT_RULES}")


# ---------------------------------------------------------------------------------------------
# Receipt parsing and rendering
# ---------------------------------------------------------------------------------------------

def parse_manifest(text: str) -> List[str]:
    """Recover the offered manifest lines from a composed task text.

    This is what lets the receipt be rendered by whoever finishes a run, without the bundle object
    travelling with the task through a queue: the block was written into the task's own text, so
    the task text is the record of what was offered. Returns [] when the text carries no block.
    """
    body = str(text or "")
    start = body.find(BLOCK_START)
    if start < 0:
        return []
    end = body.find(BLOCK_END, start)
    chunk = body[start:end if end > start else len(body)]
    # Deduped on the ref, first occurrence wins. The block lists every ref once as an index line
    # and then repeats the ones it details in full, so without this the receipt would carry a
    # detailed update twice.
    seen: Dict[str, str] = {}
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        m = _REF_RE.match(line)
        if m and m.group(1) not in seen:
            seen[m.group(1)] = line
    return list(seen.values())


def parse_usage_notes(text: str) -> Dict[str, str]:
    """The run's own account of what it used, as ``{"U1": "cited in method section"}``.

    Reads only the lines under the ``Context used:`` heading, and only lines shaped as the gate
    asked for. This parses the model's own STRUCTURED report to display it back; it never gates
    behavior on the words in it, and a run that writes nothing simply produces an empty mapping
    (the receipt then says "no note from the run" rather than asserting anything).
    """
    body = str(text or "")
    idx = body.rfind(USAGE_HEADING)
    if idx < 0:
        return {}
    out: Dict[str, str] = {}
    for line in body[idx + len(USAGE_HEADING):].splitlines():
        stripped = line.strip()
        if not stripped:
            continue                   # blank lines inside the block are tolerated
        m = _REF_RE.match(stripped)
        if not m:
            if out:
                break          # the block ended and ordinary prose resumed
            continue
        ref, note = m.group(1), " ".join(m.group(2).split())
        out[ref] = note.strip(" .-")
    return out


def strip_usage_block(text: str) -> str:
    """The run's result with its raw ``Context used:`` lines removed.

    The rendered receipt replaces them, so leaving both would show the same information twice, once
    in whatever shape the model chose and once in the standard one.
    """
    body = str(text or "")
    idx = body.rfind(USAGE_HEADING)
    if idx < 0:
        return body
    head, tail = body[:idx], body[idx + len(USAGE_HEADING):]
    kept_tail: List[str] = []
    ended = False
    for line in tail.splitlines():
        if ended:
            kept_tail.append(line)
            continue
        stripped = line.strip()
        if not stripped or _REF_RE.match(stripped):
            continue
        ended = True
        kept_tail.append(line)
    return (head.rstrip() + ("\n" + "\n".join(kept_tail).strip() if any(
        ln.strip() for ln in kept_tail) else "")).rstrip()


def render_receipt(manifest_lines: Sequence[str], usage: Dict[str, str]) -> str:
    """The standard block appended to a result: one line per offered update, what came of it.

    Every offered ref appears, in the order it was offered, whether or not the run mentioned it.
    That is the whole value: a person scanning this can see at a glance that their comment was
    read, and can see just as clearly when it was not.
    """
    rows = [ln for ln in (manifest_lines or []) if _REF_RE.match(str(ln).strip())]
    if not rows:
        return ""
    out = ["Context updates taken into account:"]
    for line in rows:
        m = _REF_RE.match(str(line).strip())
        ref = m.group(1) if m else ""
        note = (usage or {}).get(ref, "")
        if not note:
            note = "no note from the run"
        out.append(f"{line.strip()} -> {note}")
    return "\n".join(out)


def append_receipt(reported: str, manifest_lines: Sequence[str], *,
                   run_output: Optional[str] = None) -> str:
    """``reported`` with its raw usage lines replaced by the rendered receipt.

    The one call a consumer needs at the end of a run: it reads the run's own account, renders the
    standard block, and returns the text to report. With no manifest it returns the text
    unchanged, so wiring it in cannot alter a run that carried no updates.

    ``run_output`` names where the run's own account is when that is not the text being reported:
    a deep run's summary is rewritten into a report before it is sent, and that rewrite is free to
    drop the usage lines, so they are read from the raw summary.
    """
    lines = list(manifest_lines or [])
    if not lines:
        return reported
    usage = parse_usage_notes(reported if run_output is None else run_output)
    receipt = render_receipt(lines, usage)
    if not receipt:
        return reported
    return strip_usage_block(reported).rstrip() + "\n\n" + receipt


# ---------------------------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------------------------

class Watermarks:
    """Per (card, source) "an assistant last looked at this" timestamps, JSON-backed.

    Deliberately per SOURCE and not one stamp per card. The channels move at different speeds and
    are consumed by different surfaces: an attended chat may read the quest's notes without ever
    touching the document comments, and a single card-wide stamp would then mark the comments as
    seen because something else was. Per source, each channel advances only when that channel was
    actually delivered.

    A missing file, an unreadable one and a card never seen before all read as "never looked",
    which the engine turns into the bounded first-look window rather than into everything or
    nothing.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(path) if path else None
        self._data: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    @staticmethod
    def _key(card_id: str, source: str) -> str:
        return f"{card_id}:{source}"

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text())
            seen = payload.get("last_seen") if isinstance(payload, dict) else None
            if isinstance(seen, dict):
                self._data = {str(k): str(v) for k, v in seen.items()}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("context watermarks unreadable (%s); starting fresh", e)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"last_seen": self._data}, indent=2, sort_keys=True))
            os.replace(tmp, self._path)   # atomic: never a partial file after a crash
        except OSError as e:
            log.warning("could not persist context watermarks: %s", e)

    def get(self, card_id: str, source: str) -> Optional[datetime]:
        with self._lock:
            return _as_utc(self._data.get(self._key(card_id, source)))

    def set(self, card_id: str, source: str, when: datetime) -> None:
        with self._lock:
            key = self._key(card_id, source)
            existing = _as_utc(self._data.get(key))
            # Never move a watermark backwards: two lanes reading the same card would otherwise
            # re-deliver everything between them on every alternate run.
            if existing and existing >= when:
                return
            self._data[key] = when.astimezone(timezone.utc).isoformat()
            self._save()


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------

@dataclass
class CollectRequest:
    """Everything a source needs to answer "what is new for this card".

    ``card`` is whatever the caller calls a unit of work (a quest row, a goal row, a task row).
    Sources read from it by key and tolerate absence; nothing here requires a particular schema,
    which is what lets the same source serve a quest-shaped caller and a card-shaped one.
    """
    card: Dict[str, Any] = field(default_factory=dict)
    card_id: str = ""
    card_kind: str = "quest"
    # The caller's own short label for this card. Passed through because the CALLER usually has a
    # better one than the card row does: an autopilot pass already resolved a display label, while
    # the quest state endpoint returns an outcome sentence and no name at all.
    card_label: str = ""
    spec: Dict[str, Any] = field(default_factory=dict)
    since: Optional[datetime] = None
    # Whether ``since`` is a real "a run was last handed this" stamp (False) or the bounded
    # first-look window for a card nothing has ever read (True). Sources that reason about what
    # happened SINCE a delivery need the difference: on a first look nothing has been delivered.
    first_look: bool = False
    now: datetime = field(default_factory=_utcnow)
    client: Any = None
    # Shared across every source ONE ``UpdateEngine`` invokes within ``CACHE_TTL_SECONDS`` (see
    # ``UpdateEngine._cache``), not per call: a user-scoped source (reflections, insights) reads
    # once and every subsequent card in the same pass narrows the same read in memory instead of
    # re-fetching it. The default factory only matters for a ``CollectRequest`` built standalone
    # (a test, or a source called outside the engine); ``UpdateEngine.collect`` always passes its
    # own dict explicitly.
    cache: Dict[str, Any] = field(default_factory=dict)

    def opt(self, key: str, default: Any = None) -> Any:
        """One option from this card's spec for this source."""
        return self.spec.get(key, default)



def _card_label(card: Dict[str, Any]) -> str:
    """A short human label for a card: its name or title, never its outcome/description.

    An outcome is a sentence about the future and a description is a paragraph; either one in a
    one-line manifest column crowds out the information the column exists for.
    """
    for key in ("name", "title", "quest_name", "label"):
        value = str((card or {}).get(key) or "").strip()
        if value:
            return value
    return ""


class ContextSource(Protocol):
    """One channel this engine can check. Implementations must never raise; the engine catches
    anyway, but a source that reports its own empty result can say WHY."""

    name: str

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        ...


class _BaseSource:
    """Shared plumbing: a name, a one-line description for ``describe_sources``."""
    name = ""
    describes = ""
    # Whether this source's items should be put to a relevance judgment before they reach a run.
    #
    # The split is CARD-SCOPED vs USER-SCOPED, and it is the whole rule. A note on this quest, or a
    # comment on a document this card owns, is addressed to this work by construction: it is
    # relevant because of WHERE it was written, and no judgment is needed or wanted. A capture the
    # person made on their phone is user-scoped -- it is about whatever was on their mind, which is
    # usually some other part of their life -- so it arrives at every card equally and only some of
    # it bears on any one of them.
    judge_relevance = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:  # pragma: no cover
        raise NotImplementedError


class QuestNotesSource(_BaseSource):
    """Notes the PERSON added to this quest that no assistant has answered yet.

    Only the person's own notes, and that is the point: a quest an assistant writes a summary note
    to every day would otherwise report its own output back to itself as news. Attribution comes
    from the backend's ``author_kind``; a note with none is left out of the updates rather than
    guessed at, since asserting an unattributed note is the person's instruction is the one error
    with real consequences here.

    OPEN UNTIL ANSWERED, same rule as ``DriveCommentsSource``. A person's note is answered once an
    assistant note follows it on the quest. A note with an assistant note after it is history, not
    news, and a note with none is still waiting however old it is. Two live failures this
    replaces: a first look offered ten notes answered days earlier, every one marked "needs an
    answer"; and a time-filtered note was lost for good the moment one pass saw it and did nothing.

    AND NEWER THAN THE WATERMARK, even when an assistant note follows it. The watermark moves only
    when a run was handed the notes, so a note newer than it has never been in front of any run.
    An assistant note after it does not change that: a run that started before the note arrived
    and posted its summary an hour later never saw it, and treating that summary as the answer is
    the one way this source could lose a note for good. Such a note is offered once, without the
    "needs an answer" flag, so the run can see it was written and judge whether the note after it
    actually answered it. Not on a first look: with no delivery on record, "newer than the
    watermark" is just "recent", and the answered notes of the last two weeks are history.
    """
    name = "quest_notes"
    describes = "notes the person added to this quest that have no assistant reply yet"

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        quest_id = request.card_id
        lister = getattr(client, "list_quest_notes", None)
        if not callable(lister) or not quest_id:
            return []
        out: List[ContextUpdate] = []
        where = request.card_label or _card_label(request.card) or "this quest"
        notes = lister(quest_id) or []
        open_notes = self.open_notes(notes)
        offered = _still_open(open_notes, request.now, lambda n: _as_utc(n.get("created_at")))
        open_ids = {id(n) for n in offered}
        if not request.first_look:
            answered = [n for n in self.persons(notes) if id(n) not in {id(o) for o in open_notes}]
            for n in answered:
                when = _as_utc(n.get("created_at"))
                if not (request.since and when and when <= request.since):
                    offered.append(n)          # arrived after the last delivery: never seen
        for note in offered:
            when = _as_utc(note.get("created_at"))
            is_new = not (request.since and when and when <= request.since)
            is_open = id(note) in open_ids
            text = str(note.get("text") or "").strip()
            author = str(note.get("author_name") or "").strip()
            title = f"{author or 'The person'} wrote on the quest"
            if not is_new:
                title += " (still open from before)"
            elif not is_open:
                title += " (an assistant note followed it; check that it was actually answered)"
            out.append(ContextUpdate(
                source=self.name,
                kind="note",
                item_id=str(note.get("id") or note.get("note_id") or ""),
                title=title,
                body=text,
                excerpt=text,
                author=author,
                occurred_at=when,
                location=where,
                how_to_respond="add a note on this quest",
                needs_response=is_open,
                raw=dict(note or {}),
            ))
        return out

    @staticmethod
    def persons(notes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The person's own notes with text, oldest first (by ``created_at``, then list order)."""
        return [n for n in _by_time(notes) if str(n.get("author_kind") or "").lower() == "user"]

    @staticmethod
    def open_notes(notes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The person's notes with no assistant note after them, oldest first, unbounded."""
        open_notes: List[Dict[str, Any]] = []
        for note in _by_time(notes):
            kind = str(note.get("author_kind") or "").lower()
            if kind == "user":
                open_notes.append(note)
            elif kind == "ai":
                open_notes = []
        return open_notes

    @classmethod
    def unanswered(cls, notes: Sequence[Dict[str, Any]],
                   now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """``open_notes`` bounded to the ones still worth offering (see ``_still_open``)."""
        return _still_open(cls.open_notes(notes), now, lambda n: _as_utc(n.get("created_at")))


def _by_time(notes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Notes with text, oldest first by ``created_at``; an unreadable timestamp keeps list order."""
    rows = []
    for i, note in enumerate(notes or []):
        if not isinstance(note, dict) or not str(note.get("text") or "").strip():
            continue
        rows.append((_as_utc(note.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                     i, note))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [n for _, _, n in rows]


def _still_open(items: List[Any], now: Optional[datetime],
                when_of: Callable[[Any], Optional[datetime]]) -> List[Any]:
    """The open items still worth offering: none older than ``OPEN_ITEM_MAX_AGE_DAYS``, and at
    most the newest ``MAX_OPEN_PER_SOURCE``. ``items`` oldest first; an undated item counts as
    fresh. Without this, one unanswered note from last year rides into every brief forever."""
    if not items:
        return []
    floor = (now or _utcnow()) - timedelta(days=OPEN_ITEM_MAX_AGE_DAYS)
    fresh = [it for it in items if (when_of(it) or floor) >= floor]
    return fresh[-MAX_OPEN_PER_SOURCE:]


class ReflectionsSource(_BaseSource):
    """The person's latest daily/period reflection (``runner.reflections``).

    Wrapped rather than reimplemented: the module already handles the period order, the framing and
    the degradation to empty. What this adds is that it now arrives through the same engine, with a
    ref, so a run can be asked what it did with it.
    """
    name = "reflections"
    describes = "the person's own daily and period reflections"

    slot = "reflection"

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        from .reflections import collect_reflections, DEFAULT_PERIODS
        periods = tuple(request.opt("periods") or DEFAULT_PERIODS)
        # Reflections are USER-scoped, so a pass covering five quests would otherwise fetch the
        # same two documents five times. Cached on the engine (see ``CACHE_TTL_SECONDS``).
        key = ("reflections", periods)
        ctx = request.cache.get(key)
        if ctx is None:
            ctx = collect_reflections(request.client, periods=periods, now=request.now)
            request.cache[key] = ctx
        text = ctx.as_text() if ctx else ""
        if not text:
            return []
        when = _as_utc(ctx.daily_date) or request.now
        return [ContextUpdate(
            source=self.name,
            kind="reflection",
            item_id=str(ctx.daily_date or ctx.period or "latest"),
            title="Their own reflection, most recent on record",
            body=text,
            occurred_at=when,
            location="Quest reflections",
            slot=self.slot,
            needs_response=False,
            summary=ctx.one_line(),
            raw={"period": ctx.period, "daily_date": ctx.daily_date},
        )]


class InsightsSource(_BaseSource):
    """The person's unacted captures (``runner.insights``), ONE UPDATE PER CAPTURE.

    One row each, and that is the whole design: the relevance judge decides on each capture on its
    own, the receipt answers for each capture in the person's own words, and a card that names
    ``categories`` gets the captures the person tagged that way flagged as waiting on an answer
    ("is this one yours?"). Delivered as one block, none of that is possible: a judge shown the
    block as a single item either drops every capture for this card or passes every one through,
    and a receipt line for "the captures" tells nobody whether THEIR capture reached the run.

    The rows render through the composer's ``insights`` slot inside the same framing the block has
    always carried (``runner.insights.block_header`` / ``BLOCK_FOOTER``), so a brief reads as it did.

    Category matching is case-insensitive and substring-based on the PERSON's own tags. That is a
    string rule, and it is allowed because it reads their words against their words: the card's
    spec was written to match the tags they type. Nothing matches model output, and a tag never
    gates delivery.
    """
    name = "insights"
    describes = "captures the person made and has not acted on (optionally by category)"
    # The one source that reaches every card with material about all the others. Reflections are
    # exempt on purpose: there is exactly one, it is the person's steer for the day rather than an
    # item to action, and asking a model whether someone's own reflection is "relevant" to their
    # own work is a judgment with no upside.
    judge_relevance = True

    slot = "insights"

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        from .insights import collect_unacted_insights
        wanted = [str(c).strip().lower()
                  for c in (request.opt("categories") or []) if str(c).strip()]
        # User-scoped, so read once per engine and narrowed per card in memory -- the same
        # arrangement ``AutopilotPass`` had, kept because a pass over five quests should not make
        # five identical round trips.
        ctx = request.cache.get("insights")
        if ctx is None:
            ctx = collect_unacted_insights(request.client, now=request.now)
            request.cache["insights"] = ctx
        narrowed = ctx.narrow_to(request.since) if hasattr(ctx, "narrow_to") else ctx
        rows = list(getattr(narrowed, "insights", None) or [])
        if not rows:
            return []
        from .insights import BLOCK_FOOTER, block_header
        frame = (block_header(getattr(narrowed, "since", None),
                              getattr(narrowed, "window_days", 14)), BLOCK_FOOTER)
        out: List[ContextUpdate] = []
        for row in rows:
            text = str(getattr(row, "text", "") or "").strip()
            if not text:
                continue
            cats = [str(c) for c in (getattr(row, "categories", None) or [])]
            low = [c.lower() for c in cats]
            tagged_for_this = bool(wanted) and any(w in c or c in w for w in wanted for c in low)
            when = _as_utc(getattr(row, "created_at", None))
            date = when.strftime("%Y-%m-%d") if when else "an unrecorded date"
            tags = f" tagged {', '.join(cats)}" if cats else " (untagged)"
            out.append(ContextUpdate(
                source=self.name,
                kind="capture",
                item_id=str(getattr(row, "entry_id", "") or ""),
                title=(f"They tagged this {', '.join(cats)} and have not acted on it"
                       if tagged_for_this else "They captured this and have not acted on it"),
                # The row as the block always rendered it (date, tags, their words), so the slot
                # reads exactly as before with a ref in front of each row.
                body=f"[{date}]{tags}\n      {text}",
                excerpt=text,
                occurred_at=when,
                location="Quest insights",
                slot=self.slot,
                slot_frame=frame,
                how_to_respond=("act on it and say so, or say why it does not apply here"
                                if tagged_for_this else
                                "act on it and say so in your result, or pass over it"),
                needs_response=tagged_for_this,
                summary=f"Unacted insight from {date}{tags}: {_clip(text, 220)}",
                raw={"categories": cats, "promoted_by": wanted if tagged_for_this else []},
            ))
        return out



# Entry keys a habit/timer collection keeps for its own bookkeeping. Rendered, they are noise:
# a run does not need to know a day entry's period bounds, only what the person did that day.
_COLLECTION_INTERNAL_FIELDS = frozenset({
    "period", "period_start", "period_end", "last_activity_date", "entry_date", "completed",
    "sessions", "habit_timer", "completionType",
})


def _duration_label(seconds: Any) -> str:
    """``10573`` -> ``"2h 56m"``. A habit timer's raw seconds mean nothing at a glance."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class CollectionEntriesSource(_BaseSource):
    """New entries in one of the person's own collections -- a habit, a timer, a log.

    Spec: ``{"source": "collection", "name": "Focus on PhD Dissertation"}`` (resolved by name), or
    ``{"collection_id": "coll_..."}`` when the id is known.

    WHY A CARD WANTS THIS. A habit tracked against a piece of work IS a record of that work: the
    dissertation quest's own timer says whether the person sat down to it yesterday, for how long,
    and in how many sittings. A run composing today's brief without it is guessing at exactly the
    thing the person already measured, and will cheerfully propose a plan for a day they already
    spent three hours on.

    NOT put to a relevance judgment, and that is deliberate: unlike a capture, which arrives from a
    space covering the person's whole life, a collection reaches a card only because the card NAMED
    it. It is card-scoped by construction, so judging it could only ever lose one.

    ONE UPDATE PER COLLECTION, the new entries in its body, not one per entry. A log is one
    channel: seven days of a habit timer are one thing to take into account, and seven refs would
    be seven receipt lines each saying "noted" -- verified live, where a week of entries was the
    bulk of a bundle and the person's one capture sat at the bottom of it.
    """
    name = "collection"
    describes = "new entries in a habit, timer or log collection this work tracks"
    judge_relevance = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        lister = getattr(client, "list_collection_entries", None)
        if not callable(lister):
            return []
        collection_id, label = self._resolve(request)
        if not collection_id:
            return []
        entries = lister(collection_id) or []
        rows = entries.get("items") if isinstance(entries, dict) else entries
        if not isinstance(rows, list):
            return []
        kept = []                      # (when, rendered line, timer seconds, is a habit)
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            values = entry.get("fieldValues") or entry.get("field_values") or {}
            when = (_as_utc(values.get("entry_date")) or _as_utc(values.get("last_activity_date"))
                    or _as_utc(entry.get("createdAt")) or _as_utc(entry.get("created_at")))
            if request.since and when and when <= request.since:
                continue
            line = self._render(values)
            if not line:
                continue
            timer = values.get("habit_timer")
            seconds = timer.get("value") if isinstance(timer, dict) else timer
            try:
                seconds = int(float(seconds))
            except (TypeError, ValueError):
                seconds = 0
            kept.append((when or datetime.min.replace(tzinfo=timezone.utc), line, seconds,
                         str(entry.get("type") or "") == "habit"))
        if not kept:
            return []
        kept.sort(key=lambda k: k[0], reverse=True)
        kept = kept[:int(request.opt("max_entries") or 7)]
        newest, oldest = kept[0][0], kept[-1][0]
        span = (newest.strftime("%Y-%m-%d") if len(kept) == 1 or oldest == newest
                else f"{oldest.strftime('%Y-%m-%d')} to {newest.strftime('%Y-%m-%d')}")
        total = _duration_label(sum(k[2] for k in kept))
        excerpt = (f"{len(kept)} entr{'y' if len(kept) == 1 else 'ies'}, {span}"
                   + (f", {total} in all" if total else ""))
        return [ContextUpdate(
            source=self.name,
            kind="habit" if any(k[3] for k in kept) else "log",
            item_id=collection_id,
            title=f"{label}, as they logged it (newest first)",
            body="\n".join(k[1] for k in kept),
            excerpt=excerpt,
            occurred_at=newest,
            location=label,
            how_to_respond="",
            needs_response=False,
            summary=f"{label}: {excerpt}",
            raw={"collection_id": collection_id, "entries": len(kept)},
        )]

    @staticmethod
    def _render(values: Dict[str, Any]) -> str:
        """One readable line for one entry: the date, what was done, how long, plus any own fields."""
        parts: List[str] = []
        date = str(values.get("entry_date") or "").strip()
        if date:
            parts.append(date)
        completion = str(values.get("completionType") or "").strip()
        if completion:
            parts.append(completion)
        timer = values.get("habit_timer")
        seconds = timer.get("value") if isinstance(timer, dict) else timer
        spent = _duration_label(seconds)
        if spent:
            sessions = values.get("sessions")
            count = len(sessions) if isinstance(sessions, list) else 0
            parts.append(f"{spent} over {count} session(s)" if count else spent)
        for key, value in values.items():
            if key in _COLLECTION_INTERNAL_FIELDS:
                continue
            # Emptiness has to be checked on the RENDERED value, not the raw one: a field the
            # person left blank arrives as "", "  ", [] or a dict of empties depending on its type,
            # and an un-normalized check let "value_achieved: " through on live data.
            rendered = _clip(value, 120)
            if not rendered or rendered in ("{}", "[]", "None"):
                continue
            parts.append(f"{key}: {rendered}")
        return ", ".join(parts)

    def _resolve(self, request: CollectRequest) -> tuple:
        """(collection_id, label) from the spec, resolving a name against the person's own list."""
        wanted_id = str(request.opt("collection_id") or "").strip()
        wanted_name = str(request.opt("name") or request.opt("collection") or "").strip()
        if wanted_id and not wanted_name:
            return wanted_id, str(request.opt("label") or wanted_id)
        lister = getattr(request.client, "list_collections", None)
        if not callable(lister):
            return wanted_id, str(request.opt("label") or wanted_name or wanted_id)
        cached = request.cache.get("collections")
        if cached is None:
            cached = lister() or []
            request.cache["collections"] = cached
        for coll in cached:
            if not isinstance(coll, dict):
                continue
            cid, cname = str(coll.get("id") or ""), str(coll.get("name") or "")
            if (wanted_id and cid == wanted_id) or (
                    wanted_name and cname.strip().lower() == wanted_name.lower()):
                return cid, cname or wanted_name
        log.warning("context updates: no collection named %r", wanted_name or wanted_id)
        return "", wanted_name or wanted_id


class DriveCommentsSource(_BaseSource):
    """Comments people left on the documents or folder this card owns.

    Spec: ``{"source": "drive_comments", "folder_id": "..."}``, ``{"file_ids": [...]}``, or
    ``{"owner": "assistant@example.org"}`` -- any combination; their results are merged.
    Needs a Drive comments client, supplied once by the consumer (``UpdateEngine(drive_comments=)``)
    since minting a Google token is a deployment concern, not a card's.

    ``owner`` exists because a folder is often the wrong handle. Documents an assistant creates for
    a person are owned by the ASSISTANT's account and filed into the person's folder, so the
    credential ends up on each document and not on the folder around them: the folder listing
    returns nothing while every document is readable. "Every doc my assistant wrote" is then the
    query that works, and it keeps working as new docs are created, which a ``file_ids`` list does
    not.

    OPEN THREADS ARE FETCHED WITHOUT A TIME FILTER, on purpose, even though Drive's API would
    happily do the filtering. A watermark answers "what is new"; an unanswered question is not news
    after the first day and it is still unanswered. Filtering by time would drop it forever the
    moment one run saw it and did nothing. The watermark is used to LABEL which threads are new,
    and open threads keep their ref until they are answered or resolved -- bounded the same way
    open notes are (``OPEN_ITEM_MAX_AGE_DAYS`` / ``MAX_OPEN_PER_SOURCE``), or a thread nobody can
    answer would crowd every other update out of the bundle for good.
    """
    name = "drive_comments"
    describes = "comments people left on the documents this work owns"

    def __init__(self, comments_client: Any = None) -> None:
        self._client = comments_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = self._client
        if client is None:
            return []
        folder_id = str(request.opt("folder_id") or "").strip()
        owner = str(request.opt("owner") or "").strip()
        file_ids = [str(f).strip() for f in (request.opt("file_ids") or []) if str(f).strip()]
        max_files = int(request.opt("max_files") or 25)
        comments = []
        seen_files = set()
        if folder_id:
            comments.extend(client.comments_for_folder(folder_id, max_files=max_files))
            seen_files.update(c.file_id for c in comments)
        if owner and hasattr(client, "files_owned_by"):
            # No time filter on the file list: a document untouched for a month can still have a
            # comment added today, so filtering the FILES by modification date would hide the
            # comment that arrived on an old one.
            for f in client.files_owned_by(owner, max_files=max_files):
                if f.file_id in seen_files:
                    continue
                seen_files.add(f.file_id)
                comments.extend(client.comments_for_file(
                    f.file_id, file_name=f.file_name, file_url=f.file_url))
        for fid in file_ids:
            if fid not in seen_files:
                seen_files.add(fid)
                comments.extend(client.comments_for_file(fid))
        out: List[ContextUpdate] = []
        open_threads = [c for c in comments if c.needs_answer]
        open_threads.sort(key=lambda c: c.modified_at or c.created_at
                          or datetime.min.replace(tzinfo=timezone.utc))
        for c in _still_open(open_threads, request.now, lambda c: c.modified_at or c.created_at):
            when = c.modified_at or c.created_at
            is_new = not (request.since and when and when <= request.since)
            title = f'{c.author or "Someone"} commented on "{c.file_name or c.file_id}"'
            if not is_new:
                title += " (still open from before)"
            body = c.content
            if c.quoted_text:
                body = f'on "{c.quoted_text}": {c.content}'
            # Replies already on the thread, so a run does not answer a question a colleague
            # answered yesterday. Only threads with no reply from this credential reach here, so
            # every reply shown is someone else's.
            for r in c.replies:
                if r.content:
                    body += f' | reply from {r.author or "someone"}: {r.content}'
            out.append(ContextUpdate(
                source=self.name,
                kind="comment",
                item_id=f"{c.file_id}:{c.comment_id}",
                title=title,
                body=body,
                excerpt=c.content,
                author=c.author,
                occurred_at=when,
                location=c.file_name or c.file_id,
                url=c.file_url,
                how_to_respond=f"reply to comment {c.comment_id} on file {c.file_id}",
                needs_response=True,
                raw={"file_id": c.file_id, "comment_id": c.comment_id,
                     "quoted_text": c.quoted_text, "resolved": c.resolved},
            ))
        return out


class DriveChangesSource(_BaseSource):
    """Files in the card's folder that the person edited since an assistant last looked.

    The coarse companion to the comments source: it does not say what they think, only that the
    document moved. Enough to stop a run from writing a plan against a chapter that was rewritten
    yesterday.
    """
    name = "drive_changes"
    describes = "documents in this work's folder that changed"

    def __init__(self, comments_client: Any = None) -> None:
        self._client = comments_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = self._client
        folder_id = str(request.opt("folder_id") or "").strip()
        if client is None or not folder_id:
            return []
        changes = client.files_in_folder(folder_id, since=request.since,
                                         max_files=int(request.opt("max_files") or 25))
        out = []
        for f in changes:
            out.append(ContextUpdate(
                source=self.name,
                kind="edit",
                item_id=f.file_id,
                title=f'"{f.file_name}" changed',
                body="",
                occurred_at=f.modified_at,
                location=f.file_name,
                url=f.file_url,
                how_to_respond="re-read it before relying on anything you knew about it",
                needs_response=False,
                raw={"mime_type": f.mime_type},
            ))
        return out



# ---------------------------------------------------------------------------------------------
# Relevance: the engine's job, not the run's
# ---------------------------------------------------------------------------------------------

_RELEVANCE_PROMPT = """\
Someone is about to work on ONE piece of work. Below are things they captured recently. Captures
come from a personal quick-capture space and are about ALL parts of their life, so most of them
belong to some other piece of work.

THE WORK:
{work}

THE CAPTURES (each with the category tags the person chose for it):
{items}

Which of these could plausibly bear on THIS work? Answer with JSON and nothing else:
{{"relevant": [<numbers>]}}

How to decide:
  * Judge the SUBJECT MATTER of the work, not just its title. A one-line outcome names a
    destination, not a topic: read the whole description above to learn what this work is actually
    about, then ask whether the capture touches that.
  * A capture whose TAG names this work, or names the field this work is in, is strong evidence
    FOR it. The tags are the person's own labels for their own thinking, so a capture they filed
    under this work's subject almost always belongs here.
  * Material, contacts, observations and raw data in the work's subject area COUNT, even when the
    capture does not say what to do with them. Gathering is part of the work.
  * A capture about a different organization, a different job, or an unrelated part of their life
    does not belong here, even when it shares a word with the work.

WHEN IN DOUBT, INCLUDE IT. The costs are not symmetric: a capture shown that did not matter costs
one line in a brief, while a capture withheld that did matter is the person's own thinking silently
thrown away, and they will never know it happened. Exclude only what you are confident is about
something else.\
"""



def _describe_work(card: Dict[str, Any], label: str) -> str:
    """What this card is ABOUT, for a relevance judgment -- not just what it is called.

    An outcome is a destination ("I've completed my dissertation and have a PhD") and says nothing
    about the subject. Judged against that alone, a capture about the actual research topic reads
    as unrelated: verified live on 2026-09-10, when a capture the person had tagged for this very
    work was dropped from it. So everything the card knows about its own subject goes in.
    """
    parts = []
    if label:
        parts.append(f"Name: {label}")
    for key, heading in (("outcome", "Outcome"), ("description", "Description"),
                         ("current_state", "Current state"), ("instructions", "Standing brief")):
        value = _clip((card or {}).get(key), 600)
        if value:
            parts.append(f"{heading}: {value}")
    return "\n".join(parts) or label or "unnamed work"


def llm_relevance_judge(provider_fn: Callable[[], Any], tier: str = "balanced"
                        ) -> Callable[[str, str, Sequence[ContextUpdate]], Optional[set]]:
    """A judge that asks a model which user-scoped captures bear on THIS card.

    WHY THIS IS THE ENGINE'S JOB. Without it, every capture reaches every card and the RUN does the
    filtering, out loud, in the output a person reads: "Passed over: the Cornerstone capture
    (collaboration tracking) isn't this quest's domain." That line is the context engine's work
    showing up as the assistant's chatter. The person asked for a context engine, and an engine
    that hands over everything and lets the reader sort it out has not done the job.

    Deliberately a MODEL judgment and not a tag match against the card's name, which is what hard
    rule #3 in this repo's CLAUDE.md forbids and what ``runner/insights.py`` refuses to do: a fixed
    string rule silently drops every capture whose wording it did not anticipate ("dissertation" vs
    "thesis" vs no tag). Judging the subject is exactly the sanctioned alternative.

    BIASED TOWARD INCLUSION, and returns None on ANY failure, which the caller reads as "keep
    everything". A missing provider, an unroutable model, a timeout, or unparsable JSON must never
    be able to consume a person's own thinking on the way past: the worst case has to be a slightly
    noisier brief, never a silently emptier one.

    ``provider_fn`` is a callable rather than a provider because the engine is built before the CLI
    wraps ``cfg.model_provider`` with MultiProvider; resolving it at call time is what makes the
    judge use the routed provider rather than a raw one that 404s on half the model ids.
    """
    def _judge(work: str, _card_context: str,
               updates: Sequence[ContextUpdate]) -> Optional[set]:
        rows = list(updates or [])
        if not rows:
            return set()
        try:
            provider = provider_fn()
            if provider is None:
                return None
            from ..core.card_filter import _extract_json
            from ..core.model_registry import ModelRegistry
            try:
                model = ModelRegistry(provider).resolve_tier(tier or "balanced")
            except Exception:  # noqa: BLE001 -- an unresolvable registry must not stop the judge
                model = tier or "balanced"
            listed = "\n".join(
                f"{i}. [{', '.join(u.raw.get('categories') or []) or 'untagged'}] "
                f"{_clip(u.body or u.title, 300)}"
                for i, u in enumerate(rows, 1))
            # The work description goes in whole: ``_describe_work`` already clips each field,
            # and clipping the assembled text again cut it off before the description the judge
            # was given it for.
            prompt = _RELEVANCE_PROMPT.format(work=(work or "").strip() or "unnamed work",
                                              items=listed)
            raw = provider.answer([{"role": "user", "content": prompt}], model=model)
            text = raw if isinstance(raw, str) else str(getattr(raw, "text", raw) or "")
            verdict = json.loads(_extract_json(text) or "{}")
            wanted = verdict.get("relevant")
            if not isinstance(wanted, list):
                return None
            keep = set()
            for n in wanted:
                try:
                    idx = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(rows):
                    keep.add(rows[idx - 1].item_id or f"#{idx}")
            return keep
        except Exception as e:  # noqa: BLE001 -- see the docstring: failure means keep everything
            log.info("context updates: relevance judge unavailable (%s); keeping everything", e)
            return None

    return _judge


# ---------------------------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------------------------

# How a card says what it watches, when the caller supplies no resolver. Read from the card row
# itself so a backend that grows the field needs no code change here, and so an assistant can add
# a source by writing data rather than by editing this library.
DEFAULT_SPEC_KEYS = ("context_sources", "watch")


def default_spec_resolver(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The specs a card carries, from the card itself, or [] when it declares none.

    Looks in the card's own fields and inside its ``autopilot`` settings block (where per-quest
    configuration already lives in this library). Accepts a bare source name as shorthand:
    ``["quest_notes", {"source": "insights", "categories": ["PhD"]}]``.
    """
    found: List[Any] = []
    for holder in (card or {}, (card or {}).get("autopilot") or {}):
        if not isinstance(holder, dict):
            continue
        for key in DEFAULT_SPEC_KEYS:
            value = holder.get(key)
            if isinstance(value, list):
                found.extend(value)
    return _normalize_specs(found)


def _normalize_specs(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """``["quest_notes", {"source": "insights", ...}]`` -> a list of dicts, junk dropped."""
    specs: List[Dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str) and entry.strip():
            specs.append({"source": entry.strip()})
        elif isinstance(entry, dict) and str(entry.get("source") or "").strip():
            specs.append(dict(entry))
    return specs


def consumer_spec_resolver(
        spec_map: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> Callable[[Dict[str, Any]], List[Dict[str, Any]]]:
    """A resolver that reads the card's own specs AND a consumer-supplied ``{card_id: specs}`` map.

    The card carrying its own specs is the design (see the module docstring), and it stays the
    design: the map is merged with whatever the card declares, not substituted for it.

    The map exists because a card cannot always carry them YET. A backend has to grow the field
    before a person can set it, and until it does, every deployment is blocked on a schema change
    in another service to use any of this. Live case: quest-backend's autopilot settings reject an
    unknown key outright (422 ``extra_forbidden``), so a quest could not name a folder to watch at
    all. A consumer-side map unblocks that the same way ``quest_folder_map`` already does for a
    quest's local folder, and it keeps working afterwards as a per-deployment default under the
    card's own choices.

    Precedence, when both name the same source: the CARD wins. What a person set on the thing
    itself is more specific than what a deployment configured for it, and a config file quietly
    overriding a person's own setting is the failure this ordering exists to prevent.
    """
    table: Dict[str, List[Dict[str, Any]]] = {}
    for key, value in (spec_map or {}).items():
        if isinstance(value, list):
            table[str(key)] = [v for v in value if isinstance(v, (str, dict))]

    def _resolve(card: Dict[str, Any]) -> List[Dict[str, Any]]:
        own = default_spec_resolver(card)
        if not table:
            return own
        card_id = str((card or {}).get("quest_id") or (card or {}).get("id") or "")
        extra = _normalize_specs(table.get(card_id) or [])
        named = {str(s.get("source")) for s in own}
        return own + [s for s in extra if str(s.get("source")) not in named]

    return _resolve


class UpdateEngine:
    """The one object a caller asks "what has changed since I last looked at this card".

    Wiring, in full::

        engine = UpdateEngine(client, watermarks=Watermarks(path),
                              drive_comments=DriveComments(token_provider=...))
        bundle = engine.collect(quest, card_id=quest_id)
        text = bundle.as_prompt_block()
        ...   # hand `text` to the run
        bundle.mark_seen()

    ``always`` names sources every card gets whether or not it asked (the person's reflections are
    not a per-card opt-in: they are about the whole of their work). Everything else comes from the
    card's own specs, so two quests can watch entirely different things with no code between them.
    """

    def __init__(self, client: Any = None, *,
                 watermarks: Optional[Watermarks] = None,
                 drive_comments: Any = None,
                 sources: Optional[Sequence[ContextSource]] = None,
                 always: Sequence[str] = DEFAULT_ALWAYS,
                 spec_resolver: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
                 relevance_judge: Optional[Callable[..., Optional[set]]] = None,
                 first_look_days: int = FIRST_LOOK_DAYS,
                 max_updates: int = MAX_UPDATES,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self._client = client
        self._watermarks = watermarks or Watermarks(None)
        self._spec_resolver = spec_resolver or default_spec_resolver
        self._relevance_judge = relevance_judge
        self._always = tuple(always or ())
        self._first_look = timedelta(days=max(1, int(first_look_days)))
        self._max_updates = max(1, int(max_updates))
        self._now_fn = now_fn or _utcnow
        registry: Dict[str, ContextSource] = {}
        for src in (sources or self._builtin_sources(drive_comments)):
            registry[src.name] = src
        self._sources = registry
        # Cache for user-scoped sources (reflections, insights), keyed by each source's own choice
        # of key, cleared once it is older than ``CACHE_TTL_SECONDS`` -- see ``collect``.
        self._cache: Dict[str, Any] = {}
        self._cache_filled_at: Optional[datetime] = None

    @staticmethod
    def _builtin_sources(drive_comments: Any) -> List[ContextSource]:
        return [
            ReflectionsSource(),
            InsightsSource(),
            QuestNotesSource(),
            CollectionEntriesSource(),
            DriveCommentsSource(drive_comments),
            DriveChangesSource(drive_comments),
        ]

    def describe_sources(self) -> Dict[str, str]:
        """``{name: one-line description}`` -- the vocabulary a spec can use.

        Exposed so a consumer (or an assistant writing a spec for a card) can discover what is
        available instead of guessing a name that silently does nothing.
        """
        return {name: getattr(src, "describes", "") for name, src in sorted(self._sources.items())}

    def register(self, source: ContextSource) -> None:
        """Add a consumer's own source. The whole extension point: a deployment with a channel
        this library has never heard of implements ``collect`` and registers it, and every caller
        of the engine sees it without any of them changing."""
        self._sources[source.name] = source

    def specs_for(self, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        """This card's source specs: the always-on ones, then whatever the card declares.

        A card declaring a source that is also always-on wins, so ``{"source": "insights",
        "categories": [...]}`` refines the default rather than running twice.
        """
        declared = self._spec_resolver(card) or []
        named = {str(s.get("source")) for s in declared}
        specs = [{"source": name} for name in self._always if name not in named]
        specs.extend(declared)
        return specs


    def _apply_relevance(self, bundle: ContextUpdates, card: Dict[str, Any]) -> None:
        """Drop the user-scoped captures that do not bear on this card, before a run ever sees them.

        Only sources that asked to be judged (``judge_relevance``) take part: a note on this quest
        and a comment on this card's document are relevant because of where they were written, and
        putting those to a judgment could only ever lose one.

        Every failure mode keeps everything. A judge returning None (no provider, a timeout,
        unparsable JSON) leaves the bundle exactly as collected, so switching this on can make a
        brief noisier but never emptier.
        """
        if self._relevance_judge is None:
            return
        judged = [u for u in bundle.updates
                  if getattr(self._sources.get(u.source), "judge_relevance", False)]
        if not judged:
            return
        work = _describe_work(card, bundle.card_label)
        try:
            keep = self._relevance_judge(work, "", judged)
        except Exception as e:  # noqa: BLE001 -- a broken judge never costs the person context
            log.info("context updates: relevance judge failed (%s); keeping everything", e)
            return
        if keep is None:
            return
        # A judged row is kept under its item id, or under its 1-based position for a row with none.
        position = {id(u): f"#{i}" for i, u in enumerate(judged, 1)}
        dropped_by_source: Dict[str, int] = {}
        kept: List[ContextUpdate] = []
        for u in bundle.updates:
            if id(u) in position and (u.item_id or "") not in keep and position[id(u)] not in keep:
                dropped_by_source[u.source] = dropped_by_source.get(u.source, 0) + 1
                continue
            kept.append(u)
        bundle.updates = kept
        for report in bundle.reports:
            if report.source in dropped_by_source:
                report.set_aside = dropped_by_source[report.source]
                report.found = max(0, report.found - report.set_aside)

    def collect(self, card: Dict[str, Any], *, card_id: str = "", card_kind: str = "quest",
                since: Optional[datetime] = None,
                card_label: str = "",
                options: Optional[Dict[str, Dict[str, Any]]] = None) -> ContextUpdates:
        """Run every source this card watches and return one bundle.

        ``since`` overrides the stored watermark for EVERY source, which is what a caller with its
        own notion of "last time" (an autopilot pass holding ``last_pass_at``) passes in. Left
        None, each source uses its own stored watermark, falling back to the bounded first-look
        window for a card nothing has ever read.

        ``options`` is ``{source_name: {spec key: value}}`` merged over each matching spec for this
        call only: what the CALLER knows about this read that the card does not carry, such as
        which reflection periods fit the scope it is composing for.

        One source failing never costs the others: it is reported and the pass continues.
        """
        now = self._now_fn()
        if (self._cache_filled_at is None
                or (now - self._cache_filled_at).total_seconds() > CACHE_TTL_SECONDS):
            self._cache.clear()
            self._cache_filled_at = now
        cid = str(card_id or card.get("quest_id") or card.get("id") or "")
        bundle = ContextUpdates(
            card_id=cid,
            # _card_label, never the outcome: an outcome is a sentence about the future, and it
            # ended up as the label on every note row of a live receipt.
            card_label=card_label or _card_label(card),
            collected_at=now,
        )
        bundle._watermarks = self._watermarks
        for spec in self.specs_for(card):
            name = str(spec.get("source") or "")
            if options and isinstance(options.get(name), dict):
                spec = {**spec, **options[name]}
            source = self._sources.get(name)
            if source is None:
                bundle.reports.append(SourceReport(
                    source=name, spec=spec, error="no such source is registered"))
                log.warning("context updates: card %s asks for unknown source %r", cid, name)
                continue
            last_look = since or self._watermarks.get(cid, name)
            window = last_look or (now - self._first_look)
            report = SourceReport(source=name, spec=spec, since=window)
            try:
                found = list(source.collect(CollectRequest(
                    card=card, card_id=cid, card_kind=card_kind, spec=spec,
                    card_label=bundle.card_label, since=window, first_look=last_look is None,
                    now=now, client=self._client, cache=self._cache)) or [])
            except Exception as e:  # noqa: BLE001 -- one channel never breaks the rest
                report.error = f"{type(e).__name__}: {e}"
                log.warning("context updates: source %s failed for card %s: %s", name, cid, e)
                bundle.reports.append(report)
                continue
            report.found = len(found)
            bundle.reports.append(report)
            bundle.updates.extend(found)

        self._apply_relevance(bundle, card)

        # Newest first, undated last: an undated row is almost always a standing item (a
        # reflection, an open comment with no timestamp), and it should not displace today's news.
        bundle.updates.sort(
            key=lambda u: u.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True)
        # Things waiting on an answer are never the ones dropped by the cap.
        if len(bundle.updates) > self._max_updates:
            keep = [u for u in bundle.updates if u.needs_response][:self._max_updates]
            for u in bundle.updates:
                if len(keep) >= self._max_updates:
                    break
                if u not in keep:
                    keep.append(u)
            keep.sort(key=lambda u: u.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
            bundle.updates = keep
        for i, update in enumerate(bundle.updates, 1):
            update.ref = f"U{i}"
        return bundle


# ---------------------------------------------------------------------------------------------
# The consumer-facing factory
# ---------------------------------------------------------------------------------------------

def watermark_path_for(configured: Optional[str] = None,
                       state_path: Optional[str] = None) -> Optional[str]:
    """Where this deployment's watermarks live: what it configured, else beside its state file.

    Deriving a default from the state file matters more than it looks. Without a path the stamps
    live in memory for one process, so a restart re-offers everything inside the first-look window
    and the person reads their own note back to a run that already answered it. Beside the state
    file is where this library already keeps per-deployment bookkeeping, so the default persists
    without anybody configuring a second path.
    """
    if configured:
        return configured
    if not state_path:
        return None
    p = Path(state_path)
    return str(p.with_name(p.stem + "_context_watermarks.json"))


def build_update_engine(cfg: Any = None, client: Any = None, *,
                        state_path: Optional[str] = None) -> Optional[UpdateEngine]:
    """The engine a consumer's config asks for, or None when it asked for none.

    Duck-typed on purpose (``getattr`` with defaults, no ``RunnerConfig`` import): this module is
    about context channels, and a consumer with its own config object, or a test with a stub,
    should be able to build an engine without this file knowing what a ``RunnerConfig`` is.

    Returns None -- not an inert engine -- when ``context_updates`` is off, because every caller
    already treats a missing engine as "compose exactly as before", and an engine that collects
    nothing would still cost a pass through every source.
    """
    if cfg is not None and not getattr(cfg, "context_updates", True):
        return None
    path = watermark_path_for(getattr(cfg, "context_updates_state_path", None), state_path)
    return UpdateEngine(
        client,
        watermarks=Watermarks(path),
        drive_comments=getattr(cfg, "drive_comments", None),
        spec_resolver=consumer_spec_resolver(getattr(cfg, "context_sources_map", None)),
        # A callable, not a provider: this engine is built before the CLI wraps
        # cfg.model_provider with MultiProvider, so resolving it at CALL time is what makes the
        # judge use the routed provider instead of a raw one that 404s on half the model ids.
        relevance_judge=(llm_relevance_judge(
            lambda: getattr(cfg, "model_provider", None),
            tier=str(getattr(cfg, "context_updates_relevance_tier", "balanced") or "balanced"))
            if (cfg is not None and getattr(cfg, "context_updates_judge_relevance", True))
            else None),
        first_look_days=int(getattr(cfg, "context_updates_first_look_days", FIRST_LOOK_DAYS) or
                            FIRST_LOOK_DAYS),
    )
