"""rep_sync: keep an AI rep's Claude skill file in sync with its Quest profile, in ONE call.

A team's AI rep lives in two places that must agree:
  * on Quest, as the rep's **AI profile** (persona + learned corrections), edited in the team UI /
    chat and reachable through the Quest API; and
  * locally, as the rep's **Claude skill file** (``SKILL.md`` in the rep's skill dir), which the
    runner spawns Claude Code against to actually behave as that person.

This module makes the round-trip a single function so the runner (or a cron, or a chat) never has
to hand-render the file or hand-roll the API:

    sync_rep(client, team_id, user_id, skill_dir, direction="pull")   # Quest -> file
    sync_rep(client, team_id, user_id, skill_dir, direction="push")   # file  -> Quest (local edits only)
    sync_rep(client, team_id, user_id, skill_dir, direction="both")   # pull, then push local edits

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

Everything outside those marker pairs is the file owner's; ``pull`` never touches it.

Push sends only what changed LOCALLY since the last pull, never the whole file. Several writers
can own one rep (a cockpit job writes the persona, a runner lane mirrors the file), and Quest's
ai-profile PUT REPLACES ``learned_notes`` wholesale, so re-sending the pulled copy would erase a
correction added on Quest between pull and push, and restamp every note's ``created_at`` and
``source``. So:

  * ``pull`` records a baseline: the persona, learned notes and frontmatter ``display_name`` exactly
    as they parse out of the file it rendered, in a sidecar ``.rep_sync_state.json`` next to
    ``SKILL.md``.
  * ``push`` diffs the file against that baseline. ``persona`` and ``display_name`` are sent only
    when edited. Learned notes are MERGED: push re-GETs the live profile and applies the local diff
    (new bullets appended, deleted ids removed, edited texts updated) onto the CURRENT server list,
    so untouched notes keep their server fields and notes added on Quest meanwhile survive. No
    change means no PUT ("nothing to push").
  * No baseline (a file from before this scheme, or copied without its sidecar) means push sends
    nothing and logs a warning: that run is pull-only. The next pull writes the baseline.

The sidecar (rather than a hidden marker inside the managed block) keeps the baseline out of the
text that is parsed and pushed, so it can never leak into the persona, cannot be mangled by a
human or agent editing the file, and holds full note texts for an exact diff.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ._managed_sections import extract_between, replace_between, strip_managed_blocks

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

log = logging.getLogger("quest-ai-runner.rep_sync")

SKILL_FILE_NAME = "SKILL.md"
# Sidecar next to SKILL.md holding what the last pull rendered: push diffs against it.
BASELINE_FILE_NAME = ".rep_sync_state.json"

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
    sent_fields: List[str] = field(default_factory=list)  # profile fields a push actually sent


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
        text = " ".join(strip_managed_blocks(text).splitlines())
        lines.append(f"- {prefix}{text}")
    return "\n".join(lines)


def render_skill_file(existing: str, profile: Dict[str, Any]) -> str:
    """Render the managed sections of a skill file from a Quest AI profile, preserving the rest."""
    # A persona pushed as a whole SKILL.md carries its own managed blocks; rendering them back
    # inside the persona block nests the file in itself on every pull, so strip them first.
    persona = strip_managed_blocks(str(profile.get("persona") or ""))
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
        "persona": strip_managed_blocks(persona) if persona is not None else None,
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
    if not profile:
        # A missing/empty profile means the GET failed or no rep exists for the pair.
        # Rendering it would BLANK the local managed persona (and a later "both"-direction
        # push would write that blank up to Quest), so refuse instead of clobbering.
        raise RepSyncError(
            f"no AI profile for rep {user_id} on team {team_id} (fetch failed or rep missing); "
            "leaving the local skill file untouched")
    path = _skill_path(skill_dir)
    existing = _read_existing(path)
    rendered = render_skill_file(existing, profile)
    if rendered != existing:
        _write(path, rendered)
    write_baseline(skill_dir, baseline_from_text(rendered))
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


# --- baseline (what the last pull rendered) and the local diff ------------------

def baseline_from_text(text: str) -> Dict[str, Any]:
    """The comparable shape of a skill file: what push would read back out of it."""
    parsed = parse_skill_file(text)
    return {
        "persona": parsed.get("persona"),
        "learned_notes": [{k: n[k] for k in ("id", "text") if k in n}
                          for n in parsed.get("learned_notes") or []],
        "display_name": parsed.get("display_name"),
    }


def baseline_path(skill_dir: str) -> Path:
    return Path(skill_dir) / BASELINE_FILE_NAME


def read_baseline(skill_dir: str) -> Optional[Dict[str, Any]]:
    """The baseline the last pull recorded, or None when absent or unreadable."""
    path = baseline_path(skill_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_baseline(skill_dir: str, baseline: Dict[str, Any]) -> None:
    """Record ``baseline`` in the sidecar (skipped when unchanged, so pulls stay write-free)."""
    path = baseline_path(skill_dir)
    content = json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    _write(path, content)


@dataclass
class LearnedDiff:
    """How the local learned-notes list differs from the baseline."""
    added: List[str] = field(default_factory=list)            # new bullet texts
    removed_ids: List[str] = field(default_factory=list)
    removed_texts: List[str] = field(default_factory=list)    # id-less baseline notes deleted
    edited: Dict[str, str] = field(default_factory=dict)      # id -> new text

    def is_empty(self) -> bool:
        return not (self.added or self.removed_ids or self.removed_texts or self.edited)


def diff_learned_notes(baseline: List[Dict[str, Any]], local: List[Dict[str, Any]]) -> LearnedDiff:
    """Diff two parsed learned-note lists. Notes match by id; id-less notes match by text."""
    diff = LearnedDiff()
    base_by_id = {n["id"]: n.get("text", "") for n in baseline if n.get("id")}
    base_idless = [n.get("text", "") for n in baseline if not n.get("id")]
    local_ids = set()
    for note in local:
        nid, text = note.get("id"), note.get("text", "")
        if nid and nid in base_by_id:
            local_ids.add(nid)
            if text != base_by_id[nid]:
                diff.edited[nid] = text
        elif nid:
            # An id the last pull never rendered: hand-typed, or from another copy. Update it if
            # the server has it, else it is a new note (resolved at merge time).
            local_ids.add(nid)
            diff.edited[nid] = text
        elif text in base_idless:
            base_idless.remove(text)
        else:
            diff.added.append(text)
    diff.removed_ids = [nid for nid in base_by_id if nid not in local_ids]
    diff.removed_texts = base_idless
    return diff


def merge_learned_notes(server: List[Dict[str, Any]], diff: LearnedDiff) -> List[Dict[str, Any]]:
    """Apply a local diff onto the CURRENT server list. Untouched server notes pass through whole
    (id, created_at, source kept); new notes go up as ``{"text"}`` for the server to stamp."""
    removed_ids = set(diff.removed_ids)
    removed_texts = list(diff.removed_texts)
    merged: List[Dict[str, Any]] = []
    seen_ids = set()
    for note in server:
        note = dict(note)
        nid = note.get("id")
        if nid in removed_ids:
            continue
        if not nid and str(note.get("text", "")).strip() in removed_texts:
            removed_texts.remove(str(note.get("text", "")).strip())
            continue
        if nid in diff.edited:
            note["text"] = diff.edited[nid]
        if nid:
            seen_ids.add(nid)
        merged.append(note)
    for nid, text in diff.edited.items():
        if nid not in seen_ids and nid not in removed_ids:
            merged.append({"id": nid, "text": text})  # hand-typed id the server does not have
    merged.extend({"text": text} for text in diff.added)
    return merged


def push_skill_to_rep(client: Any, team_id: str, user_id: str, skill_dir: str) -> RepSyncResult:
    """Local -> Quest: send only what the file changed since the last pull.

    ``persona`` and frontmatter ``display_name`` go up only when they differ from the baseline the
    last pull recorded. Learned-note edits are merged onto a fresh GET of the live list (see the
    module docstring), so notes added on Quest in between survive. Nothing changed: no PUT. No
    baseline: no PUT and a warning (pull-only for this run). Raises :class:`RepSyncError` if the
    skill file is missing, or if the live profile cannot be fetched for a learned-notes merge.
    """
    path = _skill_path(skill_dir)
    if not path.exists():
        raise RepSyncError(f"no skill file to push at {path}")
    result = RepSyncResult(direction="push", user_id=user_id, skill_path=str(path))
    baseline = read_baseline(skill_dir)
    if baseline is None:
        log.warning("no sync baseline for %s (%s missing); not pushing, pull first to record one",
                    path, BASELINE_FILE_NAME)
        result.notes.append("no baseline: push skipped")
        return result

    local = baseline_from_text(_read_existing(path))
    kwargs: Dict[str, Any] = {}
    if local["persona"] is not None and local["persona"] != baseline.get("persona"):
        kwargs["persona"] = local["persona"]
    if local["display_name"] is not None and local["display_name"] != baseline.get("display_name"):
        kwargs["display_name"] = local["display_name"]
    diff = diff_learned_notes(list(baseline.get("learned_notes") or []), local["learned_notes"])
    if not diff.is_empty():
        live = client.get_ai_profile(user_id, team_id=team_id)
        if not live:
            raise RepSyncError(
                f"could not fetch the live profile of rep {user_id} on team {team_id} to merge "
                "learned notes; not pushing (a blind replace could erase server notes)")
        kwargs["learned_notes"] = merge_learned_notes(list(live.get("learned_notes") or []), diff)

    if not kwargs:
        log.info("nothing to push for %s -> rep %s", path, user_id)
        result.notes.append("nothing to push")
        return result

    response = client.update_ai_profile(user_id, team_id=team_id, **kwargs)
    if not response:
        log.warning("push of %s -> rep %s failed (empty response); baseline kept for a retry",
                    path, user_id)
        result.notes.append("push failed")
        return result
    # The pushed state is now the baseline, so a second push without a pull does not resend it.
    write_baseline(skill_dir, local)
    result.pushed = True
    result.sent_fields = sorted(kwargs)
    result.persona_len = len(kwargs.get("persona") or "")
    result.learned_count = len(kwargs.get("learned_notes") or [])
    log.info("pushed %s -> rep %s (fields %s; notes +%d -%d ~%d)", path, user_id,
             ", ".join(result.sent_fields), len(diff.added),
             len(diff.removed_ids) + len(diff.removed_texts), len(diff.edited))
    return result


def sync_rep(client: Any, team_id: str, user_id: str, skill_dir: str,
             direction: str = "pull") -> RepSyncResult:
    """The one simple entry point: keep a rep's skill file and Quest profile in sync.

    ``direction``:
      * ``"pull"`` (default): Quest is the source of truth; refresh the local skill file.
      * ``"push"``: send the file's local edits since the last pull up to Quest (never the
        whole file; see the module docstring).
      * ``"both"``: pull first (so the file reflects Quest), then push any local edit made since
        that pull. Right after a pull nothing differs, so this pushes nothing by itself.
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
            pulled=True, pushed=pushed.pushed, notes=pushed.notes, sent_fields=pushed.sent_fields,
            persona_len=pushed.persona_len, learned_count=pushed.learned_count,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
