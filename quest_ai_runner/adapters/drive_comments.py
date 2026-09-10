"""DriveComments -- read (and answer) the comments people leave on Google Drive documents.

A comment on a document is the cheapest thing a person can write and the easiest for an
assistant to never see. They leave it in the margin of the draft, anchored to the sentence it is
about, and then wait: the next run reads the document, sees the prose, and has no idea a question
was asked about paragraph three four days ago. Every other channel this library reads (a note, a
reflection, a capture) arrives through the org's own system; this one lives in the document, which
is exactly where a person writes when they are reading rather than planning.

WHAT MAKES THIS DIFFERENT FROM A READ: it is a channel, not a corpus. So the point of this module
is not that a run can SEE a comment, it is that a run can ANSWER one. Every ``DriveComment``
carries the file id and comment id needed to post a reply, the quoted text the comment is anchored
to (without which a two-word comment means nothing), the author, whether it is already resolved,
and any replies already on the thread. ``reply()`` and ``resolve()`` close the loop.

TIME IS A FIRST-CLASS PARAMETER. Drive's ``comments.list`` takes ``startModifiedTime``, so "what
has been said since the last time an assistant looked" is answered by the API rather than by
fetching everything and filtering locally. That is what lets a caller ask this source the same
question it asks every other context source (see ``runner/context_updates.py``), and get an honest
answer including "nothing new".

AUTH IS INJECTED, exactly as in ``google_drive_adapter``: this module never learns how a bearer
token is minted, it calls the ``token_provider()`` callable you supply. Reading needs
``drive.readonly``; POSTING A REPLY NEEDS A WRITE SCOPE (``COMMENT_WRITE_SCOPES``), and a
read-scoped token gets a clean error rather than a silent no-op.

HTTP is stdlib-only (``urllib.request`` + ``json``). It does not reuse ``GoogleDriveAdapter``'s
private request helpers on purpose: that class is a ``RetrievalAdapter`` whose contract is to
return ``Observation`` objects and never raise, and a comment channel needs typed rows and a write
path with real failure reporting. The ~20 lines of urllib boilerplate they have in common is
cheaper than coupling a write client to another class's internals.

Every read is best-effort and returns an empty list on failure, in line with every other context
source here: a person who has commented on nothing is the normal case, not an error. WRITES ARE
NOT best-effort -- ``reply()`` returns a result object saying plainly whether the reply landed,
because an assistant that believes it answered a person when it did not is worse than one that
knows it failed.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.drive-comments")

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# Enough to LIST comments and the files they sit on.
COMMENT_READ_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
)

# Enough to POST a reply or resolve a thread. ``drive.file`` is the narrower choice when the
# caller's credential only ever touches files it was explicitly given; plain ``drive`` is what a
# service account with folder-wide editor access uses.
COMMENT_WRITE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
)

# Same shape as ``google_drive_adapter.TokenProvider``: a callable returning a bearer token, or
# None when it cannot mint one.
TokenProvider = Callable[[], Optional[str]]

# The comment fields this module needs. Requested explicitly because Drive's default projection
# omits ``quotedFileContent`` and ``replies``, and a comment without the text it is anchored to is
# not answerable ("this bit is wrong" -- which bit?).
_COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,createdTime,modifiedTime,resolved,anchor,"
    "author(displayName,emailAddress,me),quotedFileContent(value,mimeType),"
    "replies(id,content,createdTime,action,author(displayName,emailAddress,me)))"
)

_FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink"

# Page sizes. Drive caps comments at 100/page; three pages of comment threads on one document is
# already far more than a run can act on, and the cap keeps a pathological document from stalling
# a pass.
_PAGE_SIZE = 100
_MAX_PAGES = 3

# Per-field caps. A comment is free text and an essay-length one must not push the document, the
# goals, and the instruction it is meant to inform out of the model's attention.
MAX_COMMENT_CHARS = 1500
MAX_QUOTE_CHARS = 400


def _clip(text: Any, limit: int) -> str:
    """A stripped string, truncated with an explicit marker so nothing silently disappears."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + " [...truncated]"


def _parse_time(raw: Any) -> Optional[datetime]:
    """An RFC 3339 Drive timestamp as an aware UTC datetime, or None when it cannot be read."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw or "").strip()
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


def _rfc3339(dt: datetime) -> str:
    """``dt`` as the RFC 3339 string Drive's query parameters expect."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class DriveReply:
    """One reply already on a comment thread.

    Carried so a run can see whether its own last answer is already there. Without it, a pass that
    replied yesterday reads the same open-looking thread today and answers it again, which is how
    a document ends up with three near-identical AI replies under one human question.
    """
    reply_id: str = ""
    author: str = ""
    author_is_me: bool = False       # written by the credential this client authenticates as
    content: str = ""
    created_at: Optional[datetime] = None
    action: str = ""                 # "resolve" | "reopen" | "" (a plain reply)


@dataclass
class DriveComment:
    """One comment thread on one Drive file, with everything needed to answer it.

    ``file_id`` + ``comment_id`` are the reply address. ``quoted_text`` is the passage the person
    anchored the comment to, and it is not optional context: comments are overwhelmingly written
    as deixis ("this is unclear", "cite here", "reword"), so the quote IS the subject.
    """
    file_id: str = ""
    file_name: str = ""
    file_url: str = ""
    comment_id: str = ""
    author: str = ""
    author_is_me: bool = False
    content: str = ""
    quoted_text: str = ""
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    resolved: bool = False
    replies: List[DriveReply] = field(default_factory=list)

    @property
    def answered_by_me(self) -> bool:
        """Whether the LAST word on this thread is already from this credential.

        Not "has this credential ever replied": a person who answers back after an AI reply has
        reopened the conversation, and that thread is open again.
        """
        return bool(self.replies) and self.replies[-1].author_is_me

    @property
    def needs_answer(self) -> bool:
        """Open, written by someone else, and not already answered by this credential."""
        return (not self.resolved) and (not self.author_is_me) and (not self.answered_by_me)

    def as_text(self) -> str:
        """The block a prompt can carry: what was said, about what, by whom, and how to answer it."""
        when = self.created_at.strftime("%Y-%m-%d") if self.created_at else "an unrecorded date"
        head = f'{self.author or "someone"} commented on "{self.file_name or self.file_id}" ({when})'
        if self.resolved:
            head += " [already resolved]"
        lines = [head]
        if self.quoted_text:
            lines.append(f'  on the passage: "{self.quoted_text}"')
        lines.append(f"  they wrote: {self.content}")
        for r in self.replies:
            who = "you (this assistant)" if r.author_is_me else (r.author or "someone")
            lines.append(f"  reply from {who}: {r.content}")
        lines.append(f"  to answer it: reply to comment {self.comment_id} on file {self.file_id}")
        return "\n".join(lines)


@dataclass
class DriveFileChange:
    """A file in a watched folder that changed since the caller last looked.

    The coarse companion to a comment: it says the person edited the document, not what they said
    about it. Worth surfacing anyway, because "they rewrote chapter two yesterday" changes what a
    run should do even when they left no comment at all.
    """
    file_id: str = ""
    file_name: str = ""
    file_url: str = ""
    mime_type: str = ""
    modified_at: Optional[datetime] = None


@dataclass
class ReplyResult:
    """The outcome of posting a reply. Explicitly NOT best-effort -- see the module docstring."""
    ok: bool = False
    reply_id: str = ""
    error: str = ""


class DriveComments:
    """Read and answer Drive document comments through an injected bearer token.

    ``token_provider`` mints the token (see ``google_chat_adapter.service_account_token_provider``
    for the Workspace service-account case, or ``static_token_provider`` for a token you already
    hold). Nothing here knows or cares which.
    """

    def __init__(self, token_provider: Optional[TokenProvider] = None, *,
                 timeout: float = 20.0) -> None:
        self._token_provider = token_provider
        self._timeout = timeout

    # --- HTTP ----------------------------------------------------------------------------

    def _token(self) -> Optional[str]:
        if not self._token_provider:
            return None
        try:
            return self._token_provider()
        except Exception as e:  # noqa: BLE001 -- a broken provider degrades to "no access"
            log.warning("drive comments: token provider failed: %s", e)
            return None

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{_DRIVE_API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request(self, method: str, url: str, token: str,
                 body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    @staticmethod
    def _http_error_text(e: urllib.error.HTTPError) -> str:
        """The API's own message when it sent one, so a scope problem reads as a scope problem."""
        try:
            payload = json.loads(e.read().decode("utf-8"))
            msg = ((payload.get("error") or {}).get("message") or "").strip()
            if msg:
                return f"HTTP {e.code}: {msg}"
        except Exception:  # noqa: BLE001 -- the body is optional and often absent
            pass
        return f"HTTP {e.code}"

    # --- reads ---------------------------------------------------------------------------

    def comments_for_file(self, file_id: str, *, since: Optional[datetime] = None,
                          include_resolved: bool = False,
                          file_name: str = "", file_url: str = "") -> List[DriveComment]:
        """Every comment thread on one file, newest activity first, or [] on any failure.

        ``since`` is pushed down to Drive as ``startModifiedTime``, so a thread the person replied
        to today comes back even though the comment itself was written last week -- which is the
        behavior a "what is new" caller wants, and the opposite of filtering on creation time.

        ``include_resolved`` is off by default: a resolved thread is a closed conversation, and
        carrying it in every run's context trains the reader to skim past all of them.
        """
        token = self._token()
        if not token or not file_id:
            return []
        params: Dict[str, Any] = {
            "fields": _COMMENT_FIELDS,
            "pageSize": _PAGE_SIZE,
            "includeDeleted": "false",
        }
        if since is not None:
            params["startModifiedTime"] = _rfc3339(since)

        out: List[DriveComment] = []
        page_token = None
        for _ in range(_MAX_PAGES):
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self._request(
                    "GET", self._url(f"/files/{urllib.parse.quote(file_id)}/comments", params),
                    token)
            except urllib.error.HTTPError as e:
                log.warning("drive comments: list failed for %s: %s",
                            file_id, self._http_error_text(e))
                return out
            except Exception as e:  # noqa: BLE001 -- transport failure degrades to what we have
                log.warning("drive comments: list failed for %s: %s", file_id, e)
                return out
            for row in payload.get("comments") or []:
                c = self._parse_comment(row, file_id, file_name, file_url)
                if c.resolved and not include_resolved:
                    continue
                out.append(c)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        out.sort(key=lambda c: c.modified_at or c.created_at or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)
        return out

    def comments_for_folder(self, folder_id: str, *, since: Optional[datetime] = None,
                            include_resolved: bool = False,
                            max_files: int = 25) -> List[DriveComment]:
        """Every comment across the files in one folder.

        The folder, not the file, is the unit a person thinks in ("the dissertation folder"), and
        it is the unit that survives them adding a document. ``max_files`` bounds the fan-out: one
        HTTP call per file is fine for a working folder and wrong for an archive.
        """
        comments: List[DriveComment] = []
        for f in self.files_in_folder(folder_id, max_files=max_files):
            comments.extend(self.comments_for_file(
                f.file_id, since=since, include_resolved=include_resolved,
                file_name=f.file_name, file_url=f.file_url))
        comments.sort(key=lambda c: c.modified_at or c.created_at or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)
        return comments

    def files_in_folder(self, folder_id: str, *, since: Optional[datetime] = None,
                        max_files: int = 25) -> List[DriveFileChange]:
        """The folder's files, newest first, optionally only those modified since ``since``.

        Used two ways: to fan out comment reads across a folder (no ``since``, because a file
        untouched for a month can still have a comment added today), and as its own signal that
        the person edited something (``since`` set).
        """
        token = self._token()
        if not token or not folder_id:
            return []
        q = f"'{folder_id}' in parents and trashed = false"
        if since is not None:
            q += f" and modifiedTime > '{_rfc3339(since)}'"
        params = {
            "q": q,
            "fields": f"files({_FILE_FIELDS})",
            "orderBy": "modifiedTime desc",
            "pageSize": max(1, min(int(max_files or 25), 100)),
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        try:
            payload = self._request("GET", self._url("/files", params), token)
        except urllib.error.HTTPError as e:
            log.warning("drive comments: folder list failed for %s: %s",
                        folder_id, self._http_error_text(e))
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("drive comments: folder list failed for %s: %s", folder_id, e)
            return []
        out = []
        for row in payload.get("files") or []:
            out.append(DriveFileChange(
                file_id=str(row.get("id") or ""),
                file_name=str(row.get("name") or ""),
                file_url=str(row.get("webViewLink") or ""),
                mime_type=str(row.get("mimeType") or ""),
                modified_at=_parse_time(row.get("modifiedTime")),
            ))
        return out

    def _parse_comment(self, row: Dict[str, Any], file_id: str,
                       file_name: str, file_url: str) -> DriveComment:
        author = row.get("author") or {}
        replies = []
        for r in row.get("replies") or []:
            ra = r.get("author") or {}
            replies.append(DriveReply(
                reply_id=str(r.get("id") or ""),
                author=str(ra.get("displayName") or ""),
                author_is_me=bool(ra.get("me")),
                content=_clip(r.get("content"), MAX_COMMENT_CHARS),
                created_at=_parse_time(r.get("createdTime")),
                action=str(r.get("action") or ""),
            ))
        return DriveComment(
            file_id=file_id,
            file_name=file_name,
            file_url=file_url,
            comment_id=str(row.get("id") or ""),
            author=str(author.get("displayName") or ""),
            author_is_me=bool(author.get("me")),
            content=_clip(row.get("content"), MAX_COMMENT_CHARS),
            quoted_text=_clip((row.get("quotedFileContent") or {}).get("value"), MAX_QUOTE_CHARS),
            created_at=_parse_time(row.get("createdTime")),
            modified_at=_parse_time(row.get("modifiedTime")),
            resolved=bool(row.get("resolved")),
            replies=replies,
        )

    # --- writes --------------------------------------------------------------------------

    def reply(self, file_id: str, comment_id: str, text: str, *,
              resolve: bool = False) -> ReplyResult:
        """Post a reply on one comment thread, optionally resolving it.

        Needs a write scope (``COMMENT_WRITE_SCOPES``). Returns a result rather than a bool so a
        caller can report WHY nothing landed -- a read-only token, a deleted thread and a network
        blip need different responses from whoever is watching.

        Resolving is deliberately a parameter on replying and not a bare method: closing a thread
        without saying what was done to it leaves the person with a resolved comment and no answer.
        """
        if not text or not str(text).strip():
            return ReplyResult(ok=False, error="empty reply text")
        token = self._token()
        if not token:
            return ReplyResult(ok=False, error="no Drive token available")
        body: Dict[str, Any] = {"content": str(text).strip()}
        if resolve:
            body["action"] = "resolve"
        url = self._url(
            f"/files/{urllib.parse.quote(file_id)}/comments/"
            f"{urllib.parse.quote(comment_id)}/replies",
            {"fields": "id,content,action"})
        try:
            payload = self._request("POST", url, token, body=body)
        except urllib.error.HTTPError as e:
            msg = self._http_error_text(e)
            log.warning("drive comments: reply failed on %s/%s: %s", file_id, comment_id, msg)
            return ReplyResult(ok=False, error=msg)
        except Exception as e:  # noqa: BLE001
            log.warning("drive comments: reply failed on %s/%s: %s", file_id, comment_id, e)
            return ReplyResult(ok=False, error=f"{type(e).__name__}: {e}")
        return ReplyResult(ok=True, reply_id=str(payload.get("id") or ""))

    def resolve(self, file_id: str, comment_id: str, text: str = "Resolved.") -> ReplyResult:
        """Close a thread with a short note saying so. Thin wrapper over ``reply(resolve=True)``."""
        return self.reply(file_id, comment_id, text, resolve=True)


def render_comments(comments: Sequence[DriveComment], *, header: str = "") -> str:
    """One readable block for a sequence of comments, or "" when there are none.

    Kept next to the client so every consumer renders a comment the same way, including the
    "here is how to answer it" line -- a comment surfaced without its reply address is a comment
    the run can only paraphrase back at the person.
    """
    rows = [c for c in comments if c and (c.content or c.quoted_text)]
    if not rows:
        return ""
    lines = [header or (
        "Comments people left on the documents this work owns. These are their own words, written "
        "in the margin of the document rather than as a task, so treat an open one as a question "
        "waiting on you. Answer it in the document (reply to the comment) as well as in your "
        "result, and say plainly when you disagree rather than silently not acting.")]
    for c in rows:
        lines.append(c.as_text())
    return "\n".join(lines)


def unanswered(comments: Iterable[DriveComment]) -> List[DriveComment]:
    """Just the threads still waiting on this credential (see ``DriveComment.needs_answer``)."""
    return [c for c in comments if c and c.needs_answer]
