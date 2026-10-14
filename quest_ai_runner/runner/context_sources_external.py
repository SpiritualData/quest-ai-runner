"""Context sources that read outside systems: Drive, inbound mail, Notion, Google Chat.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from datetime import datetime, timezone

from .context_update_state import CollectRequest
from .context_update_types import (
    ContextUpdate,
    KIND_UNKNOWN_SENTINEL,
    SourceGap,
    _OLDEST,
    _as_utc,
    _clip,
)
from .context_sources_quest import _still_open
from .context_sources_base import _BaseSource


def _as_list(value: Any) -> List[str]:
    """One value or many, always a list of non-empty strings. A spec key that means "which
    account" is the kind a person has several of, and a bare string must keep working."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _drive_explanation(comments: Sequence[Any], files_read: int) -> str:
    """Why a Drive read offered fewer threads than it found, in a person's words."""
    if not comments:
        if not files_read:
            return "no documents reached"
        return f"no comments on the {files_read} document(s) read"
    answered = sum(1 for c in comments if getattr(c, "answered_by_me", False))
    resolved = sum(1 for c in comments if getattr(c, "resolved", False))
    mine = sum(1 for c in comments if getattr(c, "author_is_me", False))
    bits = []
    if answered:
        bits.append(f"{answered} already answered in the document")
    if resolved:
        bits.append(f"{resolved} resolved")
    if mine:
        bits.append(f"{mine} written by this assistant")
    if not bits:
        return ""
    return f"{len(comments)} thread(s) across {files_read} document(s), " + ", ".join(bits)


class DriveCommentsSource(_BaseSource):
    """Comments people left on the documents or folder this card owns.

    Spec: ``{"source": "drive_comments", "folder_id": "..."}``, ``{"file_ids": [...]}``, or
    ``{"owner": "assistant@example.org"}`` (one address or a list of them) -- any combination;
    their results are merged and a file reached twice is read once.
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
    tracks_asks = True

    def __init__(self, comments_client: Any = None) -> None:
        self._client = comments_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = self._client
        if client is None:
            return []
        folder_id = str(request.opt("folder_id") or "").strip()
        # One person is several Google accounts: a work domain, an old personal address, a second
        # one a document happens to have been created under. A spec that could name only ONE of
        # them made the others invisible, and which address owns which document is not something
        # anybody keeps track of. A bare string still works.
        owners = _as_list(request.opt("owner"))
        file_ids = [str(f).strip() for f in (request.opt("file_ids") or []) if str(f).strip()]
        max_files = int(request.opt("max_files") or 25)
        comments = []
        seen_files = set()
        if folder_id and hasattr(client, "files_in_folder"):
            # Listed then read file by file, rather than through ``comments_for_folder``, so the
            # count of documents ACTUALLY READ is known. It was inferred from the comments before,
            # which made a folder of eight uncommented documents report "no documents reached" --
            # the one reading that sends somebody looking for a permissions problem.
            for f in client.files_in_folder(folder_id, max_files=max_files):
                if f.file_id in seen_files:
                    continue
                seen_files.add(f.file_id)
                comments.extend(client.comments_for_file(
                    f.file_id, file_name=f.file_name, file_url=f.file_url))
        elif folder_id:
            comments.extend(client.comments_for_folder(folder_id, max_files=max_files))
            seen_files.update(c.file_id for c in comments)
        if owners and hasattr(client, "files_owned_by"):
            # No time filter on the file list: a document untouched for a month can still have a
            # comment added today, so filtering the FILES by modification date would hide the
            # comment that arrived on an old one.
            for owner in owners:
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
        # Say what was read, so an empty channel is not mistaken for a broken one. "0 found" and
        # "two questions, both already answered in the document" look identical from outside, and
        # the first reading is the one people act on.
        request.account(len(comments), _drive_explanation(comments, len(seen_files)))
        open_threads.sort(key=lambda c: c.modified_at or c.created_at
                          or datetime.min.replace(tzinfo=timezone.utc))
        for c in _still_open(open_threads, request.now, lambda c: c.modified_at or c.created_at):
            when = c.modified_at or c.created_at
            is_new = not (request.since and when and when <= request.since)
            tracked = request.recorded(f"{c.file_id}:{c.comment_id}")
            title = f'{c.author or "Someone"} commented on "{c.file_name or c.file_id}"'
            if tracked is not None and tracked.kind != KIND_UNKNOWN_SENTINEL:
                title += f" ({tracked.status_line()})"
            elif not is_new:
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
                verbatim=True,  # their comment and the replies on it
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


def _mail_explanation(total: int, bulk: int, kept: int) -> str:
    """Why an inbound-mail read offered fewer messages than it fetched, in a person's words.

    Same reasoning as ``_drive_explanation``/``_notes_explanation``: "0 found" and "found nine,
    all newsletters" read identically from outside, and the first reading is the one that sends
    somebody hunting a permissions bug that is not there.
    """
    if not total:
        return "no messages arrived"
    if not bulk:
        return ""
    if not kept:
        return f"all {bulk} filtered as bulk or automated mail before any model call"
    return f"{bulk} of {total} filtered as bulk or automated mail before any model call"


class InboundMailSource(_BaseSource):
    """Mail that arrived at a mailbox this card names, from a real person asking for something.

    Spec: ``{"source": "inbound_mail", "mailbox": "support@example.org"}``. No code names a
    mailbox for any particular quest -- it is data the card carries, exactly like a Drive
    ``folder_id`` -- and needs a mail client supplied once by the consumer
    (``UpdateEngine(inbound_mail=...)``), since minting a Workspace token is a deployment concern.

    ONE ROW PER MESSAGE, for the same reason ``InsightsSource`` is one row per capture: an
    admission judgment made over a whole mailbox's worth of mail as a single block either admits
    everything or drops everything, and a receipt line for "the mail" tells the sender nothing
    about whether THEIR message reached anyone.

    TWO GATES, IN ORDER, NEITHER OF WHICH IS THE OTHER:

    1. ``adapters.inbound_mail.is_bulk_mail`` -- a deterministic header check, applied here, before
       a ``ContextUpdate`` is even built, so a newsletter never costs a model call and never
       reaches the admission judge at all. Counted in ``considered``/the report's explanation, not
       silently dropped.
    2. The engine's admission judgment (``judge_admission = True`` below), applied AFTER this
       method returns, over whatever survived the header check. That is a card-independent "is
       anybody home" question, never a "does this bear on this card" one -- see
       ``llm_admission_judge``'s own docstring for why the two must not be conflated.

    CARD-SCOPED, SO NEVER PUT TO THE RELEVANCE JUDGE (``judge_relevance`` stays False, inherited):
    mail that arrived at THIS card's own configured mailbox is relevant because of WHERE it
    arrived, exactly like a note on this quest or a comment on a document this card owns -- see
    ``_BaseSource.judge_relevance``.

    TRACKS ASKS AND RELAYS: a real person writing to a mailbox is asking for something by
    construction, so ``tracks_asks = True``, and because the reader of that ask is the sender, not
    whoever reads this quest, ``relay_to_quest = True`` posts it onto the quest as a note bearing
    their own address (``ContextUpdates.relay_admitted``) so the account's own asks record carries
    it under their name rather than staying invisible outside this engine's own bundle.

    THE SAFETY REFUSAL is enforced HERE, unconditionally, regardless of which mail client is
    wired: ``adapters.inbound_mail.refuses_as_inbound_mailbox`` raises before any client is even
    consulted, so a card that names a quest's own reply mailbox fails loudly as a reported source
    error rather than silently double-ingesting what a consumer's own reply service already polls.
    """
    name = "inbound_mail"
    describes = "mail that arrived at a mailbox this card watches, from real people (not bulk mail)"
    tracks_asks = True
    judge_admission = True
    relay_to_quest = True

    def __init__(self, mail_client: Any = None) -> None:
        self._client = mail_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        from ..adapters.inbound_mail import is_bulk_mail, refuses_as_inbound_mailbox

        mailbox = str(request.opt("mailbox") or "").strip().lower()
        if not mailbox:
            return []
        refusal = refuses_as_inbound_mailbox(mailbox)
        if refusal:
            raise ValueError(f"inbound_mail: {refusal}")
        client = self._client
        if client is None:
            return []
        max_messages = int(request.opt("max_messages") or 25)
        messages = list(client.messages_since(
            mailbox, since=request.since, max_messages=max_messages) or [])
        admitted = [m for m in messages if not is_bulk_mail(getattr(m, "headers", None) or {})]
        bulk = len(messages) - len(admitted)
        request.account(len(messages), _mail_explanation(len(messages), bulk, len(admitted)))
        out: List[ContextUpdate] = []
        for m in admitted:
            message_id = str(getattr(m, "message_id", "") or "")
            text = str(getattr(m, "body_text", "") or "").strip() or str(
                getattr(m, "subject", "") or "").strip()
            sender_email = str(getattr(m, "sender_email", "") or "").strip().lower()
            if not message_id or not text or not sender_email:
                continue
            who = str(getattr(m, "sender_name", "") or "").strip() or sender_email
            subject = str(getattr(m, "subject", "") or "").strip()
            title = f"{who} wrote to {mailbox}"
            if subject:
                title += f': "{_clip(subject, 100)}"'
            out.append(ContextUpdate(
                source=self.name,
                kind="email",
                item_id=message_id,
                title=title,
                body=text,
                verbatim=True,  # their email, as they wrote it
                excerpt=text,
                author=sender_email,
                occurred_at=_as_utc(getattr(m, "received_at", None)),
                location=mailbox,
                url=str(getattr(m, "url", "") or ""),
                how_to_respond=("reply to them directly; this message is relayed onto the quest "
                                "as a note in their own name, so the account's own asks record "
                                "carries it too"),
                needs_response=True,
                raw={"headers": dict(getattr(m, "headers", None) or {}), "subject": subject},
            ))
        return out


# ---------------------------------------------------------------------------------------------
# Notion and Google Chat: opt-in, read-only, named by the card
# ---------------------------------------------------------------------------------------------

# Bounds. A busy database or space must not push the actual work out of a run's attention, and the
# honest answer to "there was more" is to say so (the explanation), not to silently cut.
NOTION_MAX_ROWS = 50


NOTION_SNAPSHOT_MAX_ROWS = 500


NOTION_CHANGE_LINES = 12


CHAT_MAX_MESSAGES = 100


CHAT_MAX_THREADS = 10


CHAT_MAX_SPACES = 100


CHAT_MESSAGES_PER_THREAD = 30


CHAT_LINE_CHARS = 500


def notion_changes(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """``Property: old -> new`` for each property whose rendered value differs, sorted by name."""
    lines = []
    for name in sorted(set(before) | set(after)):
        old, new = str(before.get(name, "")), str(after.get(name, ""))
        if old != new:
            lines.append(f"{name}: {_clip(old, 80) or '(empty)'} -> {_clip(new, 80) or '(empty)'}")
    return lines


def notion_explanation(considered: int, reported: int, unchanged: int, truncated: bool,
                       limit: int) -> str:
    """Why a Notion read offered fewer rows than it touched, in a person's words."""
    bits = []
    if not considered:
        return "no rows were created or edited since the last look"
    if unchanged:
        bits.append(f"{unchanged} already delivered unchanged")
    if truncated:
        bits.append(f"only the {limit} most recently edited rows were read, so older changes in "
                    "the window are not shown")
    if not bits:
        return ""
    return f"{considered} row(s) touched, " + ", ".join(bits)


class NotionDatabaseSource(_BaseSource):
    """Rows created or edited in a Notion database this card names, and WHAT changed on them.

    Spec: ``{"source": "notion_database", "database": "<alias or configured id>"}`` (optionally
    ``"max_rows": 50``). No code names a database for any particular card: it is data the card
    carries, like a Drive ``folder_id``, and it must be one the deployment configured (the
    ``notion`` block's ``database_ids``), so a card can never point this at a database nobody
    connected. Needs a ``NotionAdapter`` supplied once by the consumer (``UpdateEngine(notion=)``).

    WHAT IS NEW is asked of Notion itself (``last_edited_time`` on or after the watermark). WHAT
    CHANGED is worked out by comparing each row's properties with the snapshot stored beside the
    watermark the last time a run was handed this database (``CollectRequest.remember``, written
    by ``mark_seen``), so a Status moving to "In progress" or a Deadline appearing arrives as
    ``Status: Not started -> In progress``, not just "row edited". Where it cannot know (a row
    never seen before that was not created inside the window) it says so instead of guessing.

    CARD-SCOPED BY WHERE IT IS NAMED, so never put to the relevance judge (``judge_relevance``
    stays False): the card chose this database. It does not track asks: a row changing is news,
    not a question owed an answer. Unconfigured, or naming a database that is not configured, is a
    reported gap (``SourceGap``), never an exception that hides the rest of the bundle.
    """
    name = "notion_database"
    describes = "rows created or edited in a Notion database this work names, and which properties changed"

    def __init__(self, notion_client: Any = None) -> None:
        self._client = notion_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = self._client
        if client is None:
            raise SourceGap("notion is not configured on this deployment (no notion block), so "
                            "no database can be read")
        database = str(request.opt("database") or "").strip()
        if not database:
            raise SourceGap('the spec names no database; use {"source": "notion_database", '
                            '"database": "<alias>"}')
        database_id = client.resolve_database(database)
        if not database_id:
            known = ", ".join(sorted(getattr(client, "databases", {}) or {})) or "none"
            raise SourceGap(f"database {database!r} is not one of the configured databases "
                            f"(configured: {known})")
        alias = client.alias_for(database_id) or database
        limit = max(1, min(int(request.opt("max_rows") or NOTION_MAX_ROWS), 100))
        since = request.since
        floor = since.replace(second=0, microsecond=0) if since else None
        # A failed read raises (NotionError): the engine reports it and the watermark stays put.
        fetched = client.fetch_rows(database, edited_since=since, limit=limit)

        prior = request.prior_snapshot(database_id) or {}
        prior_rows = prior.get("rows") if isinstance(prior.get("rows"), dict) else {}
        next_rows: Dict[str, Any] = dict(prior_rows)
        out: List[ContextUpdate] = []
        unchanged = 0
        for row in fetched.rows:
            edited_iso = row.edited_at.isoformat() if row.edited_at else ""
            before = prior_rows.get(row.page_id)
            next_rows[row.page_id] = {"props": dict(row.properties), "title": row.title,
                                      "edited": edited_iso}
            changes: Optional[List[str]] = None
            if isinstance(before, dict):
                changes = notion_changes(before.get("props") or {}, row.properties)
                if not changes and before.get("edited") == edited_iso:
                    unchanged += 1
                    continue
                kind = "changed" if changes else "edited"
            elif row.created_at and floor and row.created_at >= floor:
                kind = "created"
            else:
                kind = "edited"
            current = "; ".join(f"{k}: {_clip(v, 80)}"
                                for k, v in list(row.properties.items())[:NOTION_CHANGE_LINES])
            if kind == "created":
                body = "New row. " + (current or "No properties are filled in yet.")
            elif kind == "changed":
                shown = (changes or [])[:NOTION_CHANGE_LINES]
                body = "; ".join(shown)
                if changes and len(changes) > len(shown):
                    body += f"; and {len(changes) - len(shown)} more changed"
            elif before is not None:
                body = ("Edited, but none of the properties shown here changed, so the change is in "
                        "the page body or in a property this read does not render.")
            else:
                body = ("Edited since the last look. There is no earlier snapshot of this row, so "
                        "which properties changed is unknown. Now: " + (current or "no properties"))
            verb = {"created": "was added", "changed": "changed", "edited": "was edited"}[kind]
            out.append(ContextUpdate(
                source=self.name,
                kind=kind,
                item_id=row.page_id,
                title=f'"{row.title or "(untitled)"}" {verb} in {alias}',
                body=body,
                excerpt=(changes[0] if changes else row.title),
                occurred_at=row.edited_at,
                location=alias,
                url=row.url,
                how_to_respond=("read the page for its full content before relying on it; this "
                                "channel is read-only, so anything to record goes in your result "
                                "or on the quest"),
                needs_response=False,
                raw={"page_id": row.page_id, "database": alias, "changes": changes or []},
            ))
        if len(next_rows) > NOTION_SNAPSHOT_MAX_ROWS:
            newest = sorted(next_rows.items(), key=lambda kv: str((kv[1] or {}).get("edited") or ""),
                            reverse=True)[:NOTION_SNAPSHOT_MAX_ROWS]
            next_rows = dict(newest)
        request.remember(database_id, {"rows": next_rows})
        request.account(len(fetched.rows), notion_explanation(
            len(fetched.rows), len(out), unchanged, bool(fetched.truncated), limit))
        return out


def chat_explanation(total: int, own: int, blank: int, spaces_read: int, refused: List[str],
                     truncated: bool, threads_dropped: int) -> str:
    """Why a Chat read offered fewer messages than it fetched, in a person's words."""
    bits = []
    if refused:
        bits.append(f"{len(refused)} named space(s) refused (not on the allowlist)")
    if not total:
        bits.insert(0, f"no new messages in the {spaces_read} space(s) read")
        return ", ".join(bits)
    if own:
        bits.append(f"{own} written by this assistant or a Chat app, skipped")
    if blank:
        bits.append(f"{blank} with no text, skipped")
    if truncated:
        bits.append("more arrived than the cap, so the oldest in the window were left out")
    if threads_dropped:
        bits.append(f"{threads_dropped} older thread(s) not shown")
    return (f"{total} message(s) across {spaces_read} space(s)" + (", " if bits else "")
            + ", ".join(bits)) if bits else ""


class GoogleChatSource(_BaseSource):
    """New Google Chat messages in the spaces this card names. Read-only; allowlisted spaces only.

    Spec: ``{"source": "google_chat", "spaces": ["spaces/AAAA..."]}`` (optionally
    ``"max_messages": 100`` per space). OPT-IN UNRESTRICTED MODE: when the deployment's adapter was
    built with ``all_spaces = true``, ``{"source": "google_chat"}`` or ``"spaces": "all"`` reads
    every space, group chat and DM the subject belongs to that had activity since the watermark
    (bounded by ``"max_spaces"``, default 100, and the per-space message cap; truncation is
    reported). In the default restricted mode that spec is a gap naming how to opt in. Needs a ``GoogleChatAdapter`` supplied once by the consumer
    (``UpdateEngine(google_chat=)``), built from the ``google_chat`` block whose ``space_names`` is
    REQUIRED.

    TWO LOCKS ON WHICH SPACES. A space is read only if the card's spec NAMES it AND the deployment's
    allowlist (``space_names``) contains it. A domain-wide-delegation credential can see every space
    its subject is in, so a card naming a space nobody approved is refused before any request is
    made, and the refusal is reported. With no allowlist nothing is read at all.

    THE ASSISTANT NEVER HEARS ITSELF: messages from any Chat app (``sender.type == "BOT"``) or from
    a configured ``assistant_senders`` entry are skipped and counted. Messages are grouped by
    thread (one row per thread: a thread is the conversation), newest threads first, bounded per
    space, per thread and in length, and what was left out is stated. A message newer than the
    collection moment is left for the next look, so the watermark never skips one.

    CARD-SCOPED BY WHERE IT IS NAMED, so no relevance judge (the card chose these spaces), and no
    admission judge (Chat apps are dropped deterministically above). It does not track asks. It is
    read-only: nothing here can post, so a reply goes in the run's result or on the quest.
    """
    name = "google_chat"
    describes = "new messages in the Google Chat spaces this work names (read-only, allowlisted spaces only)"

    def __init__(self, chat_client: Any = None) -> None:
        self._client = chat_client

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = self._client
        if client is None:
            raise SourceGap("google chat is not configured on this deployment (no google_chat "
                            "block), so no space can be read")
        spaces = _as_list(request.opt("spaces") or request.opt("space"))
        wants_all = not spaces or (len(spaces) == 1 and spaces[0].lower() == "all")
        opted_in = bool(getattr(client, "all_spaces", False))
        if wants_all and not opted_in:
            raise SourceGap('the spec names no spaces (or asks for "all"), and this deployment has not opted in '
                            'to reading every space: name them ({"source": "google_chat", "spaces": '
                            '["spaces/<id>"]}) or set all_spaces = true in the google_chat block. '
                            'Nothing was read')
        allowed = getattr(client, "space_allowed", None)
        if not callable(allowed):
            raise SourceGap("the configured Chat client cannot enforce a space allowlist, so "
                            "no space is read")
        notes: List[str] = []
        space_cap = max(1, min(int(request.opt("max_spaces") or CHAT_MAX_SPACES), 500))
        if wants_all:
            listing = client.list_member_spaces(max_spaces=space_cap)
            if listing.error:
                raise RuntimeError(listing.error)
            cutoff = request.since.timestamp() if request.since is not None else None
            if cutoff is None:
                days = getattr(client, "lookback_days", None)
                cutoff = (request.now.timestamp() - days * 86400.0) if days is not None else None
            permitted, stale = [], 0
            for sp in listing.spaces:
                active = _as_utc(sp.get("lastActiveTime"))
                if cutoff is not None and active is not None and active.timestamp() <= cutoff:
                    stale += 1
                    continue
                if allowed(sp["name"]):
                    permitted.append(sp["name"])
            refused = []
            notes.append(f"{len(listing.spaces)} space(s) listed, {stale} with no activity since the "
                         f"last look skipped")
            if listing.truncated:
                notes.append(f"more than {space_cap} spaces exist, so only the first {space_cap} "
                             f"were checked (raise max_spaces to see more)")
            if not permitted:
                request.account(0, ", ".join(["no new messages in any space"] + notes))
                return []
        else:
            permitted, refused = [], []
            for space in dict.fromkeys(spaces):
                (permitted if allowed(space) else refused).append(space)
            if not permitted:
                raise SourceGap("refused: " + ", ".join(refused) + " not on this deployment's "
                                "google_chat allowlist (space_names), so nothing was read")
        cap = max(1, min(int(request.opt("max_messages") or CHAT_MAX_MESSAGES), 300))
        chunks, failures = [], []
        for space in permitted:
            try:
                chunks.append(client.fetch_messages_since(space, request.since, max_messages=cap))
            except Exception as e:  # noqa: BLE001 -- reported below; the watermark must not move
                failures.append(f"{space}: {e}")
        if failures:
            raise RuntimeError("could not read " + "; ".join(failures))

        is_mine = getattr(client, "is_assistant_message", lambda m: False)
        total = own = blank = 0
        truncated = False
        threads: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for chunk in chunks:
            truncated = truncated or bool(getattr(chunk, "truncated", False))
            for message in getattr(chunk, "messages", []) or []:
                total += 1
                if is_mine(message):
                    own += 1
                    continue
                text = " ".join(str(message.get("text") or "").split())
                if not text:
                    blank += 1
                    continue
                when = _as_utc(message.get("createTime"))
                if when and when > request.now:
                    continue          # arrived after this look began; the next look gets it
                sender = message.get("sender") or {}
                who = str(sender.get("displayName") or sender.get("name") or "someone")
                key = (chunk.space, str((message.get("thread") or {}).get("name") or chunk.space))
                slot = threads.setdefault(key, {"space": chunk.space,
                                                "display": chunk.display_name or chunk.space,
                                                "messages": []})
                slot["messages"].append((when, who, text))
        rows = []
        for (space, thread), slot in threads.items():
            msgs = sorted(slot["messages"], key=lambda m: m[0] or _OLDEST)
            rows.append((msgs[-1][0] or _OLDEST, space, thread, slot["display"], msgs))
        rows.sort(key=lambda r: r[0], reverse=True)
        dropped = max(0, len(rows) - CHAT_MAX_THREADS)
        out: List[ContextUpdate] = []
        for last, space, thread, display, msgs in rows[:CHAT_MAX_THREADS]:
            shown = msgs[-CHAT_MESSAGES_PER_THREAD:]
            body = " | ".join(f"{who}: {_clip(text, CHAT_LINE_CHARS)}" for _w, who, text in shown)
            if len(msgs) > len(shown):
                body = f"[{len(msgs) - len(shown)} earlier message(s) in this thread not shown] " + body
            out.append(ContextUpdate(
                source=self.name,
                kind="chat",
                item_id=thread,
                title=f"{len(msgs)} new message(s) in {display}",
                body=body,
                verbatim=True,      # their words
                excerpt=msgs[0][2],
                author=msgs[-1][1],
                occurred_at=last if last != _OLDEST else None,
                location=display,
                how_to_respond=("this channel is read-only and cannot post in Chat; answer in your "
                                "result or on the quest, and say so if someone there is waiting on it"),
                needs_response=False,
                raw={"space": space, "thread": thread, "messages": len(msgs)},
            ))
        explanation = chat_explanation(total, own, blank, len(permitted), refused, truncated, dropped)
        if notes:
            explanation = ", ".join([explanation] + notes) if explanation else ", ".join(notes)
        request.account(total, explanation)
        return out
