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

A THIRD managed block, ``QAR:MANAGED:next_steps``, holds the quest's canonical "what to do next"
(see :func:`publish_next_steps`). Unlike the notes block it is a REPLACE, never a log: it is
regenerated in place on every refresh, so the folder always holds exactly one current answer that
an attended session and the background autopilot both read instead of each reconstructing its own.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._managed_sections import extract_between, replace_between
from .quest_folder_zones import capture_human_input, ensure_folder_zones

log = logging.getLogger("quest-ai-runner.quest_folder_sync")

SYNC_FILE_NAME = "QUEST_SYNC.md"

_GOAL_START = "<!-- QAR:MANAGED:goal START -->"
_GOAL_END = "<!-- QAR:MANAGED:goal END -->"
_NOTES_START = "<!-- QAR:MANAGED:notes START -->"
_NOTES_END = "<!-- QAR:MANAGED:notes END -->"
_NEXT_STEPS_START = "<!-- QAR:MANAGED:next_steps START -->"
_NEXT_STEPS_END = "<!-- QAR:MANAGED:next_steps END -->"

# The Quest-side home of the next-steps artifact: a quest CONTEXT ENTRY carrying this exact name.
# Matching on the name is what makes the write an UPSERT (find it, PUT over it) instead of another
# row. Changing this string orphans whatever is already published under the old one, so treat it as
# part of the on-disk/on-server contract.
NEXT_STEPS_ENTRY_NAME = "Next steps (kept current by the runner)"

# The fallback marker, used ONLY when the client cannot do context entries at all (see
# ``_publish_to_quest``). Quest NOTES are append-only by API contract, so this prefix is the most a
# fallback can offer: a consumer can at least find every next-steps note and take the newest.
NEXT_STEPS_NOTE_MARKER = "[next-steps]"

# What we are willing to send to Quest in one artifact. The notes route caps text at 5000 chars
# server-side (a longer note is a 422, i.e. the refresh silently stops landing), and a next-steps
# artifact that needs more than this is not a next-steps artifact any more.
NEXT_STEPS_MAX_CHARS = 4000

_TO_PUSH_HEADING = "## Notes to push to Quest"
_TO_PUSH_PLACEHOLDER = (
    f"{_TO_PUSH_HEADING}\n\n"
    "<!-- Add one bullet per line below. Each un-synced bullet is posted as a note on Quest on\n"
    "     the next push and rewritten in place with its Quest note id; bullets already marked\n"
    "     with an id are left alone. -->\n"
)

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
_FRONTMATTER_QUEST_ID_RE = re.compile(r"^quest_id:\s*(\S+)\s*$", re.MULTILINE)
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
    #: Paths the three-zone scaffold created this call (see ``quest_folder_zones``). Empty on an
    #: already-conforming folder, which is the steady state.
    zones_created: List[str] = field(default_factory=list)
    #: Human-authored notes captured verbatim into ``human_context/from_quest/`` this call.
    human_notes_captured: List[str] = field(default_factory=list)


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
        # author_name alone is misleading: a note an AI run posted carries the ACCOUNT OWNER's
        # display name (the API key is theirs), so every note in this file read as if the person
        # had written it, including the dozen an AI wrote. Since this file is what both the person
        # and the next run read to see what has already been said, the kind has to win when the two
        # disagree. Unknown kind stays as the bare name rather than being guessed either way.
        kind = str(note.get("author_kind") or "").lower()
        name = str(note.get("author_name") or "").strip()
        if kind == "ai":
            author = f"{name}, AI" if name and name.lower() != "ai assistant" else "AI"
        else:
            author = name or kind
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


def quest_for_path(quest_folder_map: Dict[str, str], path: Any = None) -> Optional[Tuple[str, str]]:
    """The ``(quest_id, folder)`` whose mapped folder CONTAINS ``path`` (default: the cwd).

    The reverse of the folder map, so a consumer can answer "which quest am I standing in?" and
    make working inside a quest's folder mean working on that quest, with no id to type.

    The DEEPEST mapped folder wins. Quest folders nest in practice (a story folder holding a
    sub-project folder), and the enclosing quest would otherwise shadow the specific one, which is
    exactly backwards: the more specific location is the better answer.

    Returns None when the path is under no mapped folder. Never raises: an unresolvable path (a
    deleted cwd, a permission error on a symlink) means "no quest here", not a crash in a chat
    startup path.
    """
    if not quest_folder_map:
        return None
    try:
        here = Path(path).resolve() if path is not None else Path.cwd().resolve()
    except OSError:
        return None
    best: Optional[Tuple[str, str]] = None
    best_depth = -1
    for quest_id, folder in quest_folder_map.items():
        if not folder:
            continue
        try:
            root = Path(folder).resolve()
        except OSError:
            continue
        if here == root or root in here.parents:
            depth = len(root.parts)
            if depth > best_depth:
                best, best_depth = (str(quest_id), str(folder)), depth
    return best


# --- the canonical "next steps" artifact ------------------------------------------

@dataclass
class NextSteps:
    """The current recommended next action(s) for one quest: ONE answer, not a history.

    Deliberately small and flat. Everything here is written by whoever refreshed it (the autopilot
    pass, an attended session), and everything is optional except the steps themselves, so a caller
    that only knows "here are the next two things" can still publish a useful artifact.

    ``updated`` is passed IN rather than stamped here so a refresh is a pure function of its inputs:
    the same conclusion re-published produces a byte-identical file, and the tests can prove that
    without freezing the clock.
    """
    steps: List[str] = field(default_factory=list)
    # Work the previous period did not finish. Kept separate from ``steps`` because "still open from
    # last week" and "do this next" call for different decisions: the first may need re-sequencing
    # or dropping, and folding it into the list silently turns a slip into a plan.
    carrying_over: List[str] = field(default_factory=list)
    source: str = ""          # who refreshed it, e.g. "the autopilot pass"
    scope: str = ""           # the period this was written for, e.g. "day:2026-08-12"
    updated: str = ""         # an ISO date/timestamp string, supplied by the caller
    note: str = ""            # one optional line of context above the list

    def is_empty(self) -> bool:
        return not (self.steps or self.carrying_over or self.note)


@dataclass
class NextStepsResult:
    """What one :func:`publish_next_steps` call did, local side and Quest side."""
    quest_id: str
    sync_path: str
    # Where it landed on Quest: "context_entry" (a real upsert), "note" (append-only fallback), or
    # "none" (nothing was written there this refresh -- ``detail`` says why).
    quest_target: str = "none"
    quest_ref: str = ""       # the entry/note id, when the target gave us one
    created: bool = False     # True when the Quest-side object was created rather than replaced
    detail: str = ""


def render_next_steps(next_steps: NextSteps) -> str:
    """The artifact's body: a short, ordered list a human can read in five seconds.

    Numbered, because "next" implies an order and a bulleted set does not. The header line says
    when it was refreshed and by whom, since a next-steps artifact with no freshness stamp is
    exactly the stale guess this replaces.
    """
    lines = ["## Next steps"]
    stamp = ", ".join(p for p in (
        f"Refreshed {next_steps.updated}" if next_steps.updated else "",
        f"by {next_steps.source}" if next_steps.source else "",
        f"for {next_steps.scope}" if next_steps.scope else "",
    ) if p)
    if stamp:
        lines += ["", f"_{stamp}. This block is replaced on every refresh, so it is the current "
                      f"answer rather than a log._"]
    if next_steps.note:
        lines += ["", next_steps.note.strip()]
    if next_steps.steps:
        lines.append("")
        for i, step in enumerate(next_steps.steps, start=1):
            lines.append(f"{i}. {' '.join(str(step).strip().splitlines())}")
    if next_steps.carrying_over:
        lines += ["", "Carrying over, not finished in the previous period:"]
        for item in next_steps.carrying_over:
            lines.append(f"- {' '.join(str(item).strip().splitlines())}")
    if next_steps.is_empty():
        lines += ["", "_(nothing recorded yet)_"]
    return "\n".join(lines)


def write_next_steps(folder: str, quest_id: str, next_steps: NextSteps, *,
                     filename: str = SYNC_FILE_NAME) -> Path:
    """Replace the ``next_steps`` managed block in the folder's sync file. Returns its path.

    Creates the file (with frontmatter) when the folder has none yet, so a quest whose folder was
    never pulled still gets an artifact. Only the managed block is touched: every other line,
    including the human's own prose and the "Notes to push to Quest" section, is preserved exactly.
    """
    path = _sync_path(folder, filename)
    existing = _read_existing(path)
    out = _ensure_frontmatter(existing, quest_id)
    out = replace_between(out, _NEXT_STEPS_START, _NEXT_STEPS_END, render_next_steps(next_steps))
    if out != existing:
        _write(path, out)
    return path


def read_next_steps(folder: str, *, filename: str = SYNC_FILE_NAME) -> Optional[str]:
    """The current next-steps block's body text, or None when the folder has no artifact yet.

    Returned as TEXT, not a parsed :class:`NextSteps`: whoever refreshed it last may have been a
    human editing the block by hand, and re-parsing their prose back into fields would quietly drop
    whatever did not fit the shape. The consumer of this is a model prompt, which wants the prose.
    """
    text = _read_existing(_sync_path(folder, filename))
    body = extract_between(text, _NEXT_STEPS_START, _NEXT_STEPS_END)
    return body.strip() if body and body.strip() else None


def quest_id_in_folder(folder: str, *, filename: str = SYNC_FILE_NAME) -> Optional[str]:
    """The quest id this folder's sync file declares in its own frontmatter, or None.

    Every writer here stamps it at creation (``_ensure_frontmatter``), so any folder that was ever
    pulled or given a next-steps artifact already says which quest it belongs to. That makes it the
    natural SECOND answer to "which quest is this folder?" for a consumer with no configured
    ``quest_folder_map`` — an attended chat session, typically, where nobody set up an env map.

    The map stays the first answer where it exists (see :func:`quest_for_path`): it is the
    deployment's own statement about the mapping, while this is whatever the last writer stamped.
    """
    text = _read_existing(_sync_path(folder, filename))
    if not text:
        return None
    frontmatter = _FRONTMATTER_RE.match(text)
    if not frontmatter:
        return None
    found = _FRONTMATTER_QUEST_ID_RE.search(frontmatter.group(0))
    return found.group(1).strip() if found else None


def _publish_to_quest(client: Any, quest_id: str, body: str) -> Tuple[str, str, bool, str]:
    """Put ``body`` on the quest itself as ONE artifact. Returns (target, ref, created, detail).

    Quest's notes API is add + list only (there is no PATCH or DELETE on a note), so notes cannot
    hold a refreshing artifact: a daily refresh would leave a year of near-identical notes. Quest
    context entries CAN (POST/PUT/DELETE, and they are visible in the quest's own UI and fed to the
    brain as quest context), so the artifact lives there, upserted by its fixed name.

    Failure behavior is deliberately asymmetric. If the entry LISTING fails we write nothing at all
    rather than falling back to a blind create, because a blind create on a quest that already has
    the entry is precisely the note-spam this design exists to avoid; the local file is still
    current, and the next refresh retries. Only a client with no context-entry support at all falls
    back to a marker-prefixed note.
    """
    lister = getattr(client, "list_context_entries", None)
    creator = getattr(client, "create_context_entry", None)
    updater = getattr(client, "update_context_entry", None)
    if not (callable(lister) and callable(creator) and callable(updater)):
        adder = getattr(client, "add_quest_note", None)
        if not callable(adder):
            return "none", "", False, "this client can write neither context entries nor notes"
        notes = adder(quest_id, f"{NEXT_STEPS_NOTE_MARKER} {body}") or []
        if not notes:
            # ``add_quest_note`` returns [] on API failure, so an empty list is not "no notes", it
            # is "this did not land". Reporting it as published would hide a Quest side that has
            # silently stopped receiving refreshes.
            return "none", "", False, "the fallback note did not come back from the notes API"
        ref = str(notes[-1].get("note_id") or notes[-1].get("id") or "")
        return "note", ref, True, (
            "this client has no context-entry support, so the artifact was APPENDED as a "
            f"{NEXT_STEPS_NOTE_MARKER}-marked note; Quest notes cannot be updated, so repeated "
            "refreshes accumulate")
    try:
        entries = lister(quest_id) or []
    except Exception as e:  # noqa: BLE001 -- a failed read must not become a duplicating write
        return "none", "", False, (f"could not read this quest's context entries "
                                   f"({type(e).__name__}), so nothing was written to Quest this "
                                   f"refresh; the local file is still current")
    existing_id = next(
        (str(e.get("id")) for e in entries
         if str(e.get("name") or "").strip() == NEXT_STEPS_ENTRY_NAME and e.get("id")),
        None,
    )
    if existing_id:
        updated = updater(quest_id, existing_id, content=body) or {}
        if not updated.get("id"):
            return "none", existing_id, False, "the context-entry update did not come back"
        return "context_entry", existing_id, False, ""
    created = creator(quest_id, NEXT_STEPS_ENTRY_NAME, body) or {}
    ref = str(created.get("id") or "")
    if not ref:
        return "none", "", False, "the context-entry create did not come back with an id"
    return "context_entry", ref, True, ""


def publish_next_steps(client: Any, quest_id: str, folder: str, next_steps: NextSteps, *,
                       filename: str = SYNC_FILE_NAME) -> NextStepsResult:
    """Make ``next_steps`` the quest's current answer, locally AND on Quest, in one call.

    This is the single write path for the artifact, so the folder and the quest can never disagree
    about what comes next. The local file is written FIRST and unconditionally: it is the copy the
    person actually works next to, and a Quest outage must not cost them the answer.

    The Quest write never raises. A quest whose Quest-side write failed still has a correct local
    artifact and a ``detail`` saying what happened, and the next refresh tries again.
    """
    path = write_next_steps(folder, quest_id, next_steps, filename=filename)
    result = NextStepsResult(quest_id=quest_id, sync_path=str(path))
    body = render_next_steps(next_steps)
    if len(body) > NEXT_STEPS_MAX_CHARS:
        body = body[:NEXT_STEPS_MAX_CHARS].rstrip() + "\n\n(truncated; see the folder's sync file)"
    try:
        target, ref, created, detail = _publish_to_quest(client, quest_id, body)
    except Exception as e:  # noqa: BLE001 -- publishing is best-effort by contract
        log.warning("could not publish next steps for quest %s to Quest", quest_id, exc_info=True)
        result.detail = f"the Quest write failed ({type(e).__name__}: {e})"
        return result
    result.quest_target, result.quest_ref, result.created, result.detail = target, ref, created, detail
    log.info("next steps for quest %s -> %s (quest target: %s %s)",
             quest_id, path, target, ref or "-")
    return result


# --- the simple public functions --------------------------------------------------

def pull_quest_to_folder(client: Any, quest_id: str, folder: str,
                        *, filename: str = SYNC_FILE_NAME,
                        zones: bool = True) -> QuestFolderSyncResult:
    """Quest -> local: GET the quest's state + notes and (re)render the folder's sync file.

    Human-authored content outside the managed markers (including the "Notes to push" section)
    is preserved. Idempotent: pulling unchanged Quest state leaves the file byte-identical.
    Raises :class:`QuestFolderSyncError` if the quest is not found or inaccessible.

    ``zones`` (default on) also scaffolds the three-zone convention and captures the person's own
    notes verbatim -- see :mod:`quest_ai_runner.runner.quest_folder_zones`. It rides on the pull
    because this is the one moment the runner has both the folder and the notes in hand, and
    because a convention that depends on a run remembering to invoke it is not a convention.
    Pass ``zones=False`` for a pull that must touch nothing but the sync file.
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
    # After the sync file, never before: the scaffold is best-effort and must not be able to stop
    # the pull that is this function's actual contract.
    zones_created: List[str] = []
    captured: List[str] = []
    if zones:
        zones_created = ensure_folder_zones(folder).created
        captured = capture_human_input(folder, notes)
    log.info("pulled quest %s -> %s (%d notes, %d captured)",
             quest_id, path, len(notes), len(captured))
    return QuestFolderSyncResult(
        direction="pull", quest_id=quest_id, sync_path=str(path),
        pulled=True, notes_pulled=len(notes),
        zones_created=zones_created, human_notes_captured=captured,
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


# --- pushing the quest STATE back (the goal block is editable, within limits) --------

# What the goal block renders vs. what the API will accept are not the same set, and the gap is
# not ours to close. ``current_state`` is in NO role's write scope server-side, and ``strategies``
# are objects (id, title, accepted) that a list of bare titles cannot faithfully reconstruct -- a
# push from titles alone would silently drop ids and acceptance flags. So the editable surface is
# the two fields that round-trip losslessly, and an edit to either of the others is REPORTED
# rather than dropped: a person who retypes their current state deserves to be told it did not
# land, not to discover it next week.
_PUSHABLE_STATE_FIELDS = ("outcome", "completed")

_GOAL_LINE_RE = re.compile(r"^\*\*Goal:\*\*\s*(?P<outcome>.*)$")
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*\s*(?P<status>.*)$")


def parse_goal_block(text: str) -> Dict[str, Any]:
    """Read the managed goal block back into fields. Missing markers simply yield fewer keys."""
    block = extract_between(text, _GOAL_START, _GOAL_END)
    if block is None:
        return {}
    out: Dict[str, Any] = {}
    state_lines: List[str] = []
    in_state = False
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("**Current state:**"):
            in_state = True
            continue
        if line.startswith("**Accepted strategies:**"):
            in_state = False
            continue
        if in_state:
            state_lines.append(raw)
            continue
        m = _GOAL_LINE_RE.match(line)
        if m:
            out["outcome"] = m.group("outcome").strip()
            continue
        m = _STATUS_LINE_RE.match(line)
        if m:
            out["completed"] = m.group("status").strip().lower() == "completed"
    state = "\n".join(state_lines).strip()
    if state:
        out["current_state"] = state
    return out


def push_quest_state(client: Any, quest_id: str, folder: str,
                     *, filename: str = SYNC_FILE_NAME) -> Dict[str, Any]:
    """Local -> Quest: send edits made to the goal block's writable fields.

    Returns ``{"pushed": {...}, "blocked": [...], "held": str, "unwritable": [...]}``.
    ``unwritable`` names fields the person edited that no role may write through this route, so a
    caller can say so out loud instead of letting the edit evaporate.

    Best-effort and non-raising, except when the quest itself cannot be read: with nothing to
    compare against there is no way to tell an edit from the status quo, and pushing the whole
    block blind would overwrite Quest with a stale file.
    """
    path = _sync_path(folder, filename)
    if not path.exists():
        raise QuestFolderSyncError(f"no sync file to push at {path} — pull first")
    quest_resp = client.get_my_quest(quest_id) or {}
    live = quest_resp.get("state") or {}
    if not live:
        raise QuestFolderSyncError(f"quest {quest_id} not found or inaccessible")

    local = parse_goal_block(_read_existing(path))
    result: Dict[str, Any] = {"pushed": {}, "blocked": [], "held": "", "unwritable": []}
    if not local:
        return result

    if "current_state" in local and local["current_state"] != str(live.get("current_state") or "").strip():
        result["unwritable"].append("current_state")
        log.warning("current_state was edited in %s but no role may write it; left unpushed", path)

    changed = {f: local[f] for f in _PUSHABLE_STATE_FIELDS
               if f in local and local[f] != live.get(f)}
    if not changed:
        return result

    write = getattr(client, "write_quest_fields", None)
    if not callable(write):
        log.warning("client cannot write quest fields; %s left unpushed", sorted(changed))
        result["blocked"] = sorted(changed)
        return result

    resp = write(quest_id, changed) or {}
    # The endpoint reports a partial refusal in the BODY rather than by status, so a caller that
    # only checks for an exception believes every field landed. Surface both keys.
    if resp.get("ok"):
        result["pushed"] = changed
    else:
        result["blocked"] = list(resp.get("blocked") or sorted(changed))
        result["held"] = str(resp.get("held") or "")
    log.info("pushed quest state for %s: %s", quest_id, result)
    return result


def sync_quest_folder(client: Any, quest_id: str, folder: str, direction: str = "pull",
                     *, filename: str = SYNC_FILE_NAME,
                     zones: bool = True) -> QuestFolderSyncResult:
    """The one entry point: keep a quest's Quest state and a local folder in sync.

    ``direction``:
      * ``"pull"`` (default) — Quest is the source of truth for quest state; refresh the local file.
      * ``"push"`` — post any locally-added, un-synced notes up to Quest. No pull.
      * ``"both"`` — pull first (quest state/notes are current), then push (send anything the human
        or agent queued locally). Use when both sides may have changed.
    """
    direction = (direction or "pull").lower()
    if direction == "pull":
        return pull_quest_to_folder(client, quest_id, folder, filename=filename, zones=zones)
    if direction == "push":
        return push_folder_to_quest(client, quest_id, folder, filename=filename)
    if direction == "both":
        # State first, for the same reason quest_goal_sync pushes before it pulls: the goal block
        # is regenerated by the pull, so a local edit not sent first is a local edit destroyed.
        try:
            push_quest_state(client, quest_id, folder, filename=filename)
        except (QuestFolderSyncError, Exception) as e:  # noqa: BLE001 -- never fail the sync
            log.info("quest-state push for %s skipped (%s)", quest_id, e)
        pulled = pull_quest_to_folder(client, quest_id, folder, filename=filename, zones=zones)
        pushed = push_folder_to_quest(client, quest_id, folder, filename=filename)
        return QuestFolderSyncResult(
            direction="both", quest_id=quest_id, sync_path=pushed.sync_path,
            pulled=True, pushed=True,
            notes_pulled=pulled.notes_pulled, notes_pushed=pushed.notes_pushed,
            zones_created=pulled.zones_created,
            human_notes_captured=pulled.human_notes_captured,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
