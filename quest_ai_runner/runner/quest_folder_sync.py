"""quest_folder_sync — keep a Quest (a life goal / initiative, with its state and notes) and a
local folder in sync, in ONE call.

A quest that a person is actively working ON often has a local folder of real work: research,
drafts, code, a marketing plan. That folder and the quest's Quest state should agree:

    pull_quest_to_folder(client, quest_id, folder)                 # Quest -> local file
    push_folder_to_quest(client, quest_id, folder)                 # local file -> Quest (new notes)
    sync_quest_folder(client, quest_id, folder, direction="both")   # pull, then push

This reuses the repo's existing ``QuestClient`` (its account-wide quest methods: ``get_my_quest``,
``list_quest_notes``, ``add_quest_note`` — no new HTTP) and mirrors ``rep_sync.py``'s shape: a
single local file (``QUEST_SYNC.md`` by default) with runner-MANAGED sections delimited by
HTML-comment markers, so human-authored prose elsewhere in the file survives every re-render.

Managed-section format (inside the sync file)::

    ---
    quest_id: quest_c18a9d1409ff
    ---

    <!-- QAR:MANAGED:goal START -->
    **Goal:** The Super Psychic Academy reaches 100 paying clients
    **Status:** In progress
    ...
    <!-- QAR:MANAGED:goal END -->

    <!-- QAR:MANAGED:notes START -->
    ## Notes from Quest
    - <!-- id:note_1 --> [2026-07-01] (You) tested with 3 friends, no signups yet
    <!-- QAR:MANAGED:notes END -->

    ## Notes to push to Quest
    - a fresh local finding, not yet posted to Quest

``pull`` only ever touches the two managed blocks (goal state + notes-from-Quest), and scaffolds a
"## Notes to push to Quest" section on first render. Everything else — including that section's
own bullets — is the file owner's. ``push`` reads ONLY that section: any bullet without an
``<!-- id:... -->`` marker is posted as a new Quest note, then rewritten in place with the id it
was assigned, so re-running push is idempotent (already-synced bullets are left alone).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._managed_sections import extract_between, replace_between

log = logging.getLogger("quest-ai-runner.quest_folder_sync")

SYNC_FILE_NAME = "QUEST_SYNC.md"

_GOAL_START = "<!-- QAR:MANAGED:goal START -->"
_GOAL_END = "<!-- QAR:MANAGED:goal END -->"
_NOTES_START = "<!-- QAR:MANAGED:notes START -->"
_NOTES_END = "<!-- QAR:MANAGED:notes END -->"

_TO_PUSH_HEADING = "## Notes to push to Quest"
_TO_PUSH_PLACEHOLDER = (
    f"{_TO_PUSH_HEADING}\n\n"
    "<!-- Add one bullet per line below. Each un-synced bullet is posted as a note on Quest on\n"
    "     the next push and rewritten in place with its Quest note id; bullets already marked\n"
    "     with an id are left alone. -->\n"
)

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
# A synced-or-unsynced bullet: "- <!-- id:note_1 --> text" or plain "- text".
_BULLET_RE = re.compile(r"^-\s*(?:<!--\s*id:(?P<id>[^\s>]+)\s*-->\s*)?(?P<text>.*)$")


class QuestFolderSyncError(RuntimeError):
    """A quest <-> folder sync could not be completed (e.g. the sync file is missing for push)."""


@dataclass
class QuestFolderSyncResult:
    """What a sync did, for logging and for a caller (e.g. the poller) to report."""
    direction: str
    quest_id: str
    sync_path: str
    pulled: bool = False
    pushed: bool = False
    notes_pulled: int = 0
    notes_pushed: int = 0


# --- rendering (quest state + notes -> managed block text) ---------------------

def _render_goal_block(quest_state: Dict[str, Any]) -> str:
    outcome = str(quest_state.get("outcome") or "").strip()
    lines = [f"**Goal:** {outcome or '(no outcome set)'}"]
    lines.append(f"**Status:** {'Completed' if quest_state.get('completed') else 'In progress'}")
    category = str(quest_state.get("category") or "").strip()
    if category:
        subcategory = str(quest_state.get("subcategory") or "").strip()
        lines.append(f"**Category:** {category}{' / ' + subcategory if subcategory else ''}")
    current_state = str(quest_state.get("current_state") or "").strip()
    if current_state:
        lines.append("")
        lines.append("**Current state:**")
        lines.append(current_state)
    strategies = [s for s in (quest_state.get("strategies") or []) if s.get("accepted")]
    if strategies:
        lines.append("")
        lines.append("**Accepted strategies:**")
        for s in strategies:
            title = str(s.get("title") or s.get("id") or "").strip()
            if title:
                lines.append(f"- {title}")
    return "\n".join(lines)


def _render_notes_block(notes: List[Dict[str, Any]]) -> str:
    lines = ["## Notes from Quest"]
    if not notes:
        lines.append("")
        lines.append("_(no notes yet)_")
        return "\n".join(lines)
    lines.append("")
    for note in notes:
        text = " ".join(str(note.get("text", "")).strip().splitlines())
        if not text:
            continue
        nid = note.get("note_id") or note.get("id")
        prefix = f"<!-- id:{nid} --> " if nid else ""
        created = str(note.get("created_at") or "")[:10]
        author = note.get("author_name") or note.get("author_kind") or ""
        tag = " ".join(f"[{p}]" if p == created else f"({p})" for p in (created, author) if p)
        lines.append(f"- {prefix}{tag + ' ' if tag else ''}{text}")
    return "\n".join(lines)


def _ensure_frontmatter(existing: str, quest_id: str) -> str:
    if _FRONTMATTER_RE.match(existing or ""):
        return existing
    return f"---\nquest_id: {quest_id}\n---\n\n{existing or ''}"


def _ensure_to_push_section(text: str) -> str:
    if _TO_PUSH_HEADING in text:
        return text
    sep = "" if (not text or text.endswith("\n\n")) else ("\n" if text.endswith("\n") else "\n\n")
    return f"{text}{sep}{_TO_PUSH_PLACEHOLDER}"


def render_sync_file(existing: str, quest_id: str, quest_state: Dict[str, Any],
                     notes: List[Dict[str, Any]]) -> str:
    """Render the managed sections of a quest-folder sync file, preserving everything else."""
    out = _ensure_frontmatter(existing or "", quest_id)
    out = replace_between(out, _GOAL_START, _GOAL_END, _render_goal_block(quest_state))
    out = replace_between(out, _NOTES_START, _NOTES_END, _render_notes_block(notes))
    return _ensure_to_push_section(out)


# --- push helpers (parse the human-owned "Notes to push" section) --------------

def _to_push_bounds(lines: List[str]) -> Optional[Tuple[int, int]]:
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == _TO_PUSH_HEADING:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("#"):
            end = j
            break
    return start, end


# --- file helpers ----------------------------------------------------------------

def _sync_path(folder: str, filename: str) -> Path:
    return Path(folder) / filename


def _read_existing(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:  # pragma: no cover - filesystem edge
        raise QuestFolderSyncError(f"could not read sync file {path}: {e}") from e


def _write(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise QuestFolderSyncError(f"could not write sync file {path}: {e}") from e


# --- the simple public functions --------------------------------------------------

def pull_quest_to_folder(client: Any, quest_id: str, folder: str,
                        *, filename: str = SYNC_FILE_NAME) -> QuestFolderSyncResult:
    """Quest -> local: GET the quest's state + notes and (re)render the folder's sync file.

    Human-authored content outside the managed markers (including the "Notes to push" section)
    is preserved. Idempotent: pulling unchanged Quest state leaves the file byte-identical.
    Raises :class:`QuestFolderSyncError` if the quest is not found or inaccessible.
    """
    quest_resp = client.get_my_quest(quest_id) or {}
    quest_state = quest_resp.get("state") or {}
    if not quest_state:
        raise QuestFolderSyncError(f"quest {quest_id} not found or inaccessible")
    notes = list(client.list_quest_notes(quest_id) or [])
    path = _sync_path(folder, filename)
    existing = _read_existing(path)
    rendered = render_sync_file(existing, quest_id, quest_state, notes)
    if rendered != existing:
        _write(path, rendered)
    log.info("pulled quest %s -> %s (%d notes)", quest_id, path, len(notes))
    return QuestFolderSyncResult(
        direction="pull", quest_id=quest_id, sync_path=str(path),
        pulled=True, notes_pulled=len(notes),
    )


def push_folder_to_quest(client: Any, quest_id: str, folder: str,
                        *, filename: str = SYNC_FILE_NAME) -> QuestFolderSyncResult:
    """Local -> Quest: post any un-synced bullet in "Notes to push to Quest" as a new Quest note.

    Each pushed bullet is rewritten in place with the id the note was assigned, so a repeated
    push only ever sends bullets added since the last one. Raises :class:`QuestFolderSyncError`
    if the sync file doesn't exist yet (nothing to push from — pull first).
    """
    path = _sync_path(folder, filename)
    if not path.exists():
        raise QuestFolderSyncError(f"no sync file to push at {path} — pull first")
    text = _read_existing(path)
    lines = text.splitlines()
    bounds = _to_push_bounds(lines)
    pushed = 0
    if bounds is not None:
        start, end = bounds
        for i in range(start, end):
            stripped = lines[i].strip()
            if not stripped.startswith("-"):
                continue
            m = _BULLET_RE.match(stripped)
            if not m or m.group("id"):
                continue  # not a bullet, or already synced
            bullet_text = (m.group("text") or "").strip()
            if not bullet_text:
                continue
            updated_notes = client.add_quest_note(quest_id, bullet_text) or []
            if not updated_notes:
                # add_quest_note returns [] on API failure: the note may or may not have landed,
                # but without an id we can't mark the bullet. Leave it un-synced for the next push
                # and DON'T count it — logging here beats silently claiming it was pushed.
                log.warning("push of bullet %r to quest %s got no notes back; left un-synced",
                            bullet_text[:80], quest_id)
                continue
            # Find the id of the note we just added: exact-text match first, else fall back to the
            # LAST note (the list is oldest -> newest, so the just-appended note is last). The
            # fallback matters: if the server trims/rewrites the text, an unmarked bullet would be
            # re-posted as a duplicate on EVERY subsequent push.
            new_id = next(
                (n.get("note_id") or n.get("id") for n in reversed(updated_notes)
                 if str(n.get("text", "")).strip() == bullet_text),
                None,
            ) or (updated_notes[-1].get("note_id") or updated_notes[-1].get("id"))
            marker = f"<!-- id:{new_id} --> " if new_id else ""
            lines[i] = f"- {marker}{bullet_text}"
            pushed += 1
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if new_text != text:
        _write(path, new_text)
    log.info("pushed %d note(s) from %s -> quest %s", pushed, path, quest_id)
    return QuestFolderSyncResult(
        direction="push", quest_id=quest_id, sync_path=str(path),
        pushed=True, notes_pushed=pushed,
    )


def sync_quest_folder(client: Any, quest_id: str, folder: str, direction: str = "pull",
                     *, filename: str = SYNC_FILE_NAME) -> QuestFolderSyncResult:
    """The one entry point: keep a quest's Quest state and a local folder in sync.

    ``direction``:
      * ``"pull"`` (default) — Quest is the source of truth for quest state; refresh the local file.
      * ``"push"`` — post any locally-added, un-synced notes up to Quest. No pull.
      * ``"both"`` — pull first (quest state/notes are current), then push (send anything the human
        or agent queued locally). Use when both sides may have changed.
    """
    direction = (direction or "pull").lower()
    if direction == "pull":
        return pull_quest_to_folder(client, quest_id, folder, filename=filename)
    if direction == "push":
        return push_folder_to_quest(client, quest_id, folder, filename=filename)
    if direction == "both":
        pulled = pull_quest_to_folder(client, quest_id, folder, filename=filename)
        pushed = push_folder_to_quest(client, quest_id, folder, filename=filename)
        return QuestFolderSyncResult(
            direction="both", quest_id=quest_id, sync_path=pushed.sync_path,
            pulled=True, pushed=True,
            notes_pulled=pulled.notes_pulled, notes_pushed=pushed.notes_pushed,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
