"""rep_sync — keep an AI rep's Claude skill file in sync with its Quest profile, in ONE call.

A team's AI rep lives in two places that must agree:
  * on Quest, as the rep's **AI profile** (persona + learned corrections), edited in the team UI /
    chat and reachable through the Quest API; and
  * locally, as the rep's **Claude skill file** (``SKILL.md`` in the rep's skill dir), which the
    runner spawns Claude Code against to actually behave as that person.

This module makes the round-trip a single function so the runner (or a cron, or a chat) never has
to hand-render the file or hand-roll the API:

    sync_rep(client, team_id, user_id, skill_dir, direction="pull")   # Quest -> file
    sync_rep(client, team_id, user_id, skill_dir, direction="push")   # file  -> Quest
    sync_rep(client, team_id, user_id, skill_dir, direction="both")   # pull, then push back

It reuses the repo's existing ``QuestClient`` (no new HTTP) and writes only the runner-MANAGED
sections of the skill file, delimited by HTML-comment markers, so any human-authored prose in the
file is preserved across a re-render (the sync is idempotent over the managed regions only).

Managed-section format (inside the skill file)::

    <!-- QAR:MANAGED:persona START -->
    ...profile.persona, verbatim...
    <!-- QAR:MANAGED:persona END -->

    <!-- QAR:MANAGED:learned START -->
    ## Learned corrections
    <!-- one bullet per learned note; the id is carried so push can round-trip it -->
    - <!-- id:note_1 --> be concise in status updates
    - <!-- id:note_2 --> never schedule meetings on Fridays
    <!-- QAR:MANAGED:learned END -->

Everything outside those marker pairs is the file owner's; ``pull`` never touches it. ``push``
reads only those regions back and PUTs persona + learned_notes to the profile.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ._managed_sections import extract_between, replace_between

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

log = logging.getLogger("quest-ai-runner.rep_sync")

SKILL_FILE_NAME = "SKILL.md"

# Marker pairs delimiting the runner-managed regions. Anything between START and END is owned by
# the sync and re-rendered idempotently; everything else in the file is the human's and preserved.
_PERSONA_START = "<!-- QAR:MANAGED:persona START -->"
_PERSONA_END = "<!-- QAR:MANAGED:persona END -->"
_LEARNED_START = "<!-- QAR:MANAGED:learned START -->"
_LEARNED_END = "<!-- QAR:MANAGED:learned END -->"

_LEARNED_HEADING = "## Learned corrections"
# A learned bullet optionally carries its note id so a pull -> edit -> push round-trips ids:
#   - <!-- id:note_1 --> text...
_LEARNED_BULLET_RE = re.compile(r"^-\s*(?:<!--\s*id:(?P<id>[^\s>]+)\s*-->\s*)?(?P<text>.*)$")


class RepSyncError(RuntimeError):
    """A skill file / profile sync could not be completed (e.g. file unreadable)."""


@dataclass
class RepSyncResult:
    """What a sync did, for logging and for the executor's report."""
    direction: str
    user_id: str
    skill_path: str
    pulled: bool = False
    pushed: bool = False
    persona_len: int = 0
    learned_count: int = 0
    notes: List[str] = field(default_factory=list)


# --- rendering (profile -> managed block text) ----------------------------------

def _render_learned_block(learned_notes: List[Dict[str, Any]]) -> str:
    lines = [_LEARNED_HEADING]
    if not learned_notes:
        lines.append("")
        lines.append("_(no corrections recorded yet)_")
        return "\n".join(lines)
    lines.append("")
    for note in learned_notes:
        text = str(note.get("text", "")).strip()
        if not text:
            continue
        nid = note.get("id")
        prefix = f"<!-- id:{nid} --> " if nid else ""
        # Keep each note on one line so the round-trip parse stays simple.
        text = " ".join(text.splitlines())
        lines.append(f"- {prefix}{text}")
    return "\n".join(lines)


def render_skill_file(existing: str, profile: Dict[str, Any]) -> str:
    """Render the managed sections of a skill file from a Quest AI profile, preserving the rest."""
    persona = str(profile.get("persona") or "").strip()
    learned = list(profile.get("learned_notes") or [])
    out = existing or ""
    out = replace_between(out, _PERSONA_START, _PERSONA_END, persona)
    out = replace_between(out, _LEARNED_START, _LEARNED_END, _render_learned_block(learned))
    return out


# --- parsing (managed block text -> profile fields) -----------------------------


def _extract_frontmatter_metadata(text: str) -> Dict[str, Any]:
    """Extract YAML frontmatter metadata (display_name, etc.) from skill file.

    Returns a dict with frontmatter fields like {display_name, name, description, ...}.
    If frontmatter is not present or YAML parsing fails, returns an empty dict.
    """
    if not yaml:
        return {}
    try:
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        meta = yaml.safe_load(parts[1]) or {}
        return meta if isinstance(meta, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def parse_skill_file(text: str) -> Dict[str, Any]:
    """Read persona + learned_notes back out of a skill file's managed sections.

    Also extracts display_name from frontmatter if present.

    Returns ``{persona, learned_notes, display_name}`` with only what the file actually
    contains (a missing managed block yields ``None``/empty so push can send only what's present).
    """
    persona = extract_between(text, _PERSONA_START, _PERSONA_END)
    learned_raw = extract_between(text, _LEARNED_START, _LEARNED_END)
    learned_notes: List[Dict[str, Any]] = []
    if learned_raw is not None:
        for line in learned_raw.splitlines():
            line = line.rstrip()
            if not line.startswith("-"):
                continue
            m = _LEARNED_BULLET_RE.match(line)
            if not m:
                continue
            note_text = (m.group("text") or "").strip()
            if not note_text or note_text.startswith("_("):  # skip the placeholder bullet
                continue
            note: Dict[str, Any] = {"text": note_text}
            if m.group("id"):
                note["id"] = m.group("id")
            learned_notes.append(note)
    meta = _extract_frontmatter_metadata(text)
    return {
        "persona": persona.strip() if persona is not None else None,
        "learned_notes": learned_notes,
        "display_name": meta.get("display_name"),
    }


# --- file helpers ---------------------------------------------------------------

def _skill_path(skill_dir: str) -> Path:
    return Path(skill_dir) / SKILL_FILE_NAME


def _read_existing(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:  # pragma: no cover - filesystem edge
        raise RepSyncError(f"could not read skill file {path}: {e}") from e


def _write(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise RepSyncError(f"could not write skill file {path}: {e}") from e


# --- the simple public functions ------------------------------------------------

def pull_rep_to_skill(client: Any, team_id: str, user_id: str, skill_dir: str,
                      *, note_store: Any = None) -> RepSyncResult:
    """Quest -> local: GET the rep's AI profile and (re)render its skill file's managed sections.

    Human-authored content outside the managed markers is preserved. Idempotent: pulling an
    unchanged profile leaves the file byte-identical. Returns a :class:`RepSyncResult`.

    Args:
        client:     A QuestClient (or compatible) with ``get_ai_profile``.
        team_id:    The team the rep belongs to.
        user_id:    The rep's user id.
        skill_dir:  Directory containing the rep's SKILL.md.
        note_store: Optional ``NoteContextStore`` — when provided, the rep's learned_notes from
                    the just-pulled profile are synced into it.  Best-effort: a sync failure is
                    logged and never raises.
    """
    profile = client.get_ai_profile(user_id, team_id=team_id)
    path = _skill_path(skill_dir)
    existing = _read_existing(path)
    rendered = render_skill_file(existing, profile)
    if rendered != existing:
        _write(path, rendered)
    learned = list(profile.get("learned_notes") or [])
    log.info("pulled rep %s -> %s (persona %d chars, %d corrections)",
             user_id, path, len(str(profile.get("persona") or "")), len(learned))
    if note_store is not None:
        sync_notes_to_store(learned, note_store)
    return RepSyncResult(
        direction="pull", user_id=user_id, skill_path=str(path), pulled=True,
        persona_len=len(str(profile.get("persona") or "")), learned_count=len(learned),
    )


def sync_notes_to_store(learned_notes: List[Dict[str, Any]], note_store: Any) -> None:
    """Sync a list of learned_notes dicts into a NoteContextStore.  Never raises.

    A convenience helper for callers (e.g. the poller) that already have a ``note_store``
    instance and want to sync without going through ``pull_rep_to_skill``.
    """
    try:
        note_store.sync_from_notes(learned_notes)
    except Exception as e:  # noqa: BLE001
        log.warning("sync_notes_to_store failed: %s", e)


def push_skill_to_rep(client: Any, team_id: str, user_id: str, skill_dir: str) -> RepSyncResult:
    """Local -> Quest: read managed sections + frontmatter from skill file and PUT to profile.

    Sends persona, learned_notes, and display_name (if present in frontmatter).
    Only the fields the file actually carries are sent (a file with just a persona block pushes
    only the persona). Raises :class:`RepSyncError` if the skill file is missing.
    """
    path = _skill_path(skill_dir)
    if not path.exists():
        raise RepSyncError(f"no skill file to push at {path}")
    parsed = parse_skill_file(_read_existing(path))
    persona = parsed.get("persona")
    learned_notes = parsed.get("learned_notes")
    display_name = parsed.get("display_name")
    kwargs: Dict[str, Any] = {"team_id": team_id}
    if persona is not None:
        kwargs["persona"] = persona
    if learned_notes is not None:
        kwargs["learned_notes"] = learned_notes
    if display_name is not None:
        kwargs["display_name"] = display_name
    client.update_ai_profile(user_id, **kwargs)
    log.info("pushed skill %s -> rep %s (persona %s, display_name %s, %d corrections)",
             path, user_id, "set" if persona else "unchanged",
             display_name or "unchanged", len(learned_notes or []))
    return RepSyncResult(
        direction="push", user_id=user_id, skill_path=str(path), pushed=True,
        persona_len=len(persona or ""), learned_count=len(learned_notes or []),
    )


def sync_rep(client: Any, team_id: str, user_id: str, skill_dir: str,
             direction: str = "pull") -> RepSyncResult:
    """The one simple entry point: keep a rep's skill file and Quest profile in sync.

    ``direction``:
      * ``"pull"`` (default) — Quest is the source of truth; refresh the local skill file.
      * ``"push"`` — the local skill file is the source of truth; send it up to Quest.
      * ``"both"`` — pull first (so the file reflects Quest), then push (so any local managed-section
        edit that survived the merge is written back). Use when both sides may have changed.
    """
    direction = (direction or "pull").lower()
    if direction == "pull":
        return pull_rep_to_skill(client, team_id, user_id, skill_dir)
    if direction == "push":
        return push_skill_to_rep(client, team_id, user_id, skill_dir)
    if direction == "both":
        pulled = pull_rep_to_skill(client, team_id, user_id, skill_dir)
        pushed = push_skill_to_rep(client, team_id, user_id, skill_dir)
        return RepSyncResult(
            direction="both", user_id=user_id, skill_path=pushed.skill_path,
            pulled=True, pushed=True,
            persona_len=pushed.persona_len, learned_count=pushed.learned_count,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
