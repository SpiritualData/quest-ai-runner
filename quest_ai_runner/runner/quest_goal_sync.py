"""quest_goal_sync — a quest's GOALS, in a file, editable in both directions.

``QUEST_SYNC.md`` answers "what is this quest and what was said about it". It does not answer
"what are all my goals". The quest state block carries one OUTCOME, and the next-steps block
carries the two or three things to do now; the plan itself — the ladder of quarter, month, week
and day goals — lives only in Quest. A folder without it grows a hand-maintained substitute
(``goals.yaml``, ``todos.md``, whatever the consumer invents), which drifts from Quest the moment
either side changes and which no other consumer's folder has.

So: one standard file, ``GOALS.md``, and real two-way sync.

    pull_quest_goals(client, quest_id, folder)                     # Quest -> GOALS.md
    push_goals_to_quest(client, quest_id, folder)                  # GOALS.md -> Quest
    sync_quest_goals(client, quest_id, folder, direction="both")   # push, THEN pull

THE FILE. One managed block, the same HTML-comment markers the rest of this package uses, so
prose outside it survives every re-render. Inside, goals are grouped by period and rendered as
checkbox bullets carrying their id. On PULL, each bullet also carries its description (labelled
as its brief) and its most recent updates -- the per-goal check-in thread where a person writes
what they read/did/found on that one goal -- newest first, each labelled with its date and
author::

    <!-- QAR:MANAGED:goals START -->
    ## Goals

    ### Quarter
    **Q3 2026 (Jul - Sep)** <!-- period:2026_Q3 scope:quarter -->
    - [ ] <!-- id:goal_00c5922b --> Secure commitment from all committee members (due 2026-09-30)
          > Brief: Get every committee member's written sign-off before the September vote.
          > 2026-09-14 Joshua: Read the bylaws draft, two clauses need updating first.
    - [x] <!-- id:goal_bfeda075 --> Finish the concept paper
    <!-- QAR:MANAGED:goals END -->

Every detail line -- brief or update, including every continuation line of a multi-line note --
is rendered as an indented markdown blockquote. That is not decoration: it is what keeps a
person's own words from ever being read back as a hand-typed edit (see ``_render_detail`` below).
Detail lines are pull-only rendering; they carry nothing push reads, and how many updates render
is controlled by ``updates_per_goal`` (0 turns them off; descriptions still render).

THREE EDITS PUSH, and they were chosen because each is unambiguous on the page:

* **Tick a box** on a goal Quest thinks is open -> the goal is completed.
* **Change the text** after an id -> the goal is renamed.
* **Add a bullet with NO id** under a period heading -> a new goal is created in that period, and
  the bullet is rewritten in place with the id it was assigned (so a repeated push is a no-op,
  exactly like ``quest_folder_sync``'s note bullets).

Un-ticking a box does NOT reopen a goal. Reopening is a real decision with a real endpoint, and
inferring it from the absence of an ``x`` would make every rendering hiccup a silent state change.

PUSH RUNS BEFORE PULL in ``direction="both"``, which is the opposite order to
``quest_folder_sync``, and the reason is worth stating: the edits above live INSIDE the managed
block, and a pull regenerates that block from Quest. Pulling first would erase the tick before it
was ever sent. Pushing first sends it, and the pull that follows brings back Quest's own view of
what just happened -- so the round trip is self-correcting rather than lossy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._managed_sections import replace_between

log = logging.getLogger("quest-ai-runner.quest_goal_sync")

GOALS_FILE_NAME = "GOALS.md"

_GOALS_START = "<!-- QAR:MANAGED:goals START -->"
_GOALS_END = "<!-- QAR:MANAGED:goals END -->"

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

# "- [x] <!-- id:goal_abc --> Title (due 2026-09-30)"; every part after the box optional so a
# hand-typed "- [ ] new goal" still parses as a creation.
_GOAL_LINE_RE = re.compile(
    r"^-\s*\[(?P<box>[ xX])\]\s*(?:<!--\s*id:(?P<id>[^\s>]+)\s*-->\s*)?(?P<text>.*)$")
_PERIOD_RE = re.compile(
    r"^\*\*(?P<label>.*?)\*\*\s*<!--\s*period:(?P<period>\S+)(?:\s+scope:(?P<scope>\S+))?\s*-->\s*$")
_DUE_RE = re.compile(r"\s*\(due\s+(?P<due>\d{4}-\d{2}-\d{2})\)\s*$")

# Six spaces (aligns under "- [ ] ", itself six characters) plus a blockquote marker. Every
# rendered detail line -- a goal's brief, one of its updates, and every continuation line of a
# multi-line note -- gets exactly this prefix. That is what makes a detail line unparseable as a
# goal edit: after ``parse_goal_edits`` calls ``raw.strip()``, the line still starts with ">", so
# it can never match ``_GOAL_LINE_RE`` (needs a leading "-") or ``_PERIOD_RE`` (needs a leading
# "**") no matter what text a person put inside their own note.
_DETAIL_INDENT = "      > "


class QuestGoalSyncError(RuntimeError):
    """Raised when a goal sync cannot proceed (quest inaccessible, no file to push from)."""


@dataclass
class GoalSyncResult:
    """What one sync did, for logging and for a caller to report."""
    direction: str
    quest_id: str
    goals_path: str
    pulled: bool = False
    pushed: bool = False
    goals_rendered: int = 0
    completed: List[str] = field(default_factory=list)
    renamed: List[str] = field(default_factory=list)
    created: List[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.completed) + len(self.renamed) + len(self.created)


@dataclass
class GoalEdits:
    """Local edits parsed out of the managed block, before anything is sent."""
    completed: List[str] = field(default_factory=list)              # goal ids
    renamed: List[Tuple[str, str]] = field(default_factory=list)     # (goal id, new title)
    created: List[Dict[str, Any]] = field(default_factory=list)      # {period, scope, title, ...}

    def is_empty(self) -> bool:
        return not (self.completed or self.renamed or self.created)


# --- rendering ---------------------------------------------------------------

def _goal_title(goal: Dict[str, Any]) -> str:
    """The goal's display text. ``name`` and ``title`` are both in play across endpoints."""
    return str(goal.get("name") or goal.get("title") or "").strip()


def _render_goal_line(goal: Dict[str, Any]) -> Optional[str]:
    title = _goal_title(goal)
    if not title:
        return None
    gid = str(goal.get("id") or goal.get("goal_id") or "").strip()
    marker = f"<!-- id:{gid} --> " if gid else ""
    box = "x" if goal.get("completed") else " "
    due = str(goal.get("deadline") or "").strip()
    return f"- [{box}] {marker}{title}{f' (due {due})' if due else ''}"


def _render_detail(label: str, text: str) -> List[str]:
    """One detail (a description, or a single update) as indented blockquote lines.

    Every physical line gets its own ``_DETAIL_INDENT`` prefix -- the label line AND every
    continuation line of a multi-line note. Prefixing only the first line would let a note whose
    SECOND line happens to read like ``- [x] ...`` or ``**Q3 2026** <!-- period:... -->`` reach
    the file unindented, where a push would read it as a real edit.
    """
    physical = (text or "").split("\n")
    out = [f"{_DETAIL_INDENT}{label}{physical[0]}"]
    out.extend(f"{_DETAIL_INDENT}{ln}" for ln in physical[1:])
    return out


def _format_update_label(update: Dict[str, Any]) -> str:
    """Renders as ``YYYY-MM-DD Author: `` -- date first (it sorts/scans better than a name)."""
    date = str(update.get("createdAt") or "").strip()[:10]
    author = str(update.get("userName") or "").strip() or "unknown"
    return f"{date} {author}: " if date else f"{author}: "


def _render_goal_details(goal: Dict[str, Any], updates: List[Dict[str, Any]],
                         updates_per_goal: int) -> List[str]:
    """The lines under one goal bullet: its brief, then its updates, newest first.

    ``updates`` is expected newest-first (the client contract); only the first ``updates_per_goal``
    are rendered. Full text, never truncated -- these are a person's own words.
    """
    lines: List[str] = []
    description = str(goal.get("description") or "").strip()
    if description:
        lines.extend(_render_detail("Brief: ", description))
    if updates_per_goal > 0:
        for update in updates[:updates_per_goal]:
            note = str(update.get("note") or "")
            if not note.strip():
                continue
            lines.extend(_render_detail(_format_update_label(update), note))
    return lines


def render_goals_block(goal_data: Dict[str, Any], *,
                       updates_by_goal: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                       updates_per_goal: int = 3) -> str:
    """The managed block: every goal, grouped by period, ids carried inline.

    Completed goals are kept and ticked rather than dropped. A plan that silently loses its
    finished rows reads, a quarter later, as though the work was never scheduled -- and it would
    also make the file disagree with Quest about what exists, which is what breaks push.

    ``updates_by_goal`` (goal id -> that goal's updates, newest first) and ``updates_per_goal``
    control the detail lines rendered under each bullet -- see ``_render_goal_details``. Both are
    pull-only: push never calls this with anything but the defaults' worth of goal data.
    """
    updates_by_goal = updates_by_goal or {}
    groups = goal_data.get("period_groups") or []
    lines = ["## Goals"]
    if not groups:
        return "\n".join(lines + ["", "_(no goals yet)_"])

    total = sum(len(g.get("goals") or []) for g in groups)
    done = sum(1 for g in groups for x in (g.get("goals") or []) if x.get("completed"))
    lines += ["", f"_{total} goal(s) across {len(groups)} period(s); {done} completed._", ""]

    last_scope = None
    for group in groups:
        goals = group.get("goals") or []
        if not goals:
            continue
        scope = str(group.get("time_scope") or "").strip()
        if scope and scope != last_scope:
            lines.append(f"### {scope.capitalize()}")
            last_scope = scope
        label = str(group.get("period_label") or group.get("period") or "").strip()
        period = str(group.get("period") or "").strip()
        if label:
            # The period KEY rides in a comment beside the human label: a new bullet typed under
            # this heading has to be creatable, and "Q3 2026 (Jul - Sep)" is not what the API takes.
            meta = f" <!-- period:{period}{f' scope:{scope}' if scope else ''} -->" if period else ""
            lines.append(f"**{label}**{meta}")
        for goal in goals:
            line = _render_goal_line(goal)
            if line:
                lines.append(line)
                gid = str(goal.get("id") or goal.get("goal_id") or "").strip()
                lines.extend(_render_goal_details(goal, updates_by_goal.get(gid) or [],
                                                  updates_per_goal))
        lines.append("")
    return "\n".join(lines).rstrip()


def _ensure_frontmatter(existing: str, quest_id: str) -> str:
    if _FRONTMATTER_RE.match(existing or ""):
        return existing
    return f"---\nquest_id: {quest_id}\n---\n\n{existing or ''}"


def render_goals_file(existing: str, quest_id: str, goal_data: Dict[str, Any], *,
                      updates_by_goal: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                      updates_per_goal: int = 3) -> str:
    """Render the managed block into the goals file, preserving everything else."""
    out = _ensure_frontmatter(existing or "", quest_id)
    block = render_goals_block(goal_data, updates_by_goal=updates_by_goal,
                               updates_per_goal=updates_per_goal)
    return replace_between(out, _GOALS_START, _GOALS_END, block)


# --- parsing local edits -----------------------------------------------------

def _split_due(text: str) -> Tuple[str, Optional[str]]:
    """Separate a trailing ``(due YYYY-MM-DD)`` from the title."""
    m = _DUE_RE.search(text)
    if not m:
        return text.strip(), None
    return text[:m.start()].strip(), m.group("due")


def parse_goal_edits(text: str, known: Dict[str, Dict[str, Any]]) -> GoalEdits:
    """Read the managed block and return only what genuinely differs from ``known``.

    ``known`` maps goal id -> the goal as Quest last reported it. Comparing against it is what
    keeps a push idempotent: re-pushing an unchanged file sends nothing, so a sync loop cannot
    turn into a write loop.
    """
    edits = GoalEdits()
    period, scope = "", ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        pm = _PERIOD_RE.match(line)
        if pm:
            period, scope = pm.group("period"), (pm.group("scope") or "")
            continue
        gm = _GOAL_LINE_RE.match(line)
        if not gm:
            continue
        title, due = _split_due(gm.group("text") or "")
        if not title:
            continue
        gid = gm.group("id")
        ticked = gm.group("box").lower() == "x"
        if not gid:
            # No id: a goal someone typed. Needs a period to live in; without one there is no
            # honest guess, so it is skipped loudly rather than filed somewhere arbitrary.
            if not period:
                log.warning("goal %r has no id and sits under no period heading; skipped", title)
                continue
            edits.created.append({"title": title, "period": period,
                                  "scope": scope, "deadline": due})
            continue
        prior = known.get(gid)
        if prior is None:
            log.warning("goal id %s is in the file but not in Quest; ignoring", gid)
            continue
        if ticked and not prior.get("completed"):
            edits.completed.append(gid)
        # Un-ticking never reopens: see the module docstring.
        if title != _goal_title(prior):
            edits.renamed.append((gid, title))
    return edits


# --- file helpers ------------------------------------------------------------

def _goals_path(folder: str, filename: str = GOALS_FILE_NAME) -> Path:
    return Path(folder).expanduser() / filename


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")


def _known_goals(goal_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for group in goal_data.get("period_groups") or []:
        for goal in group.get("goals") or []:
            gid = str(goal.get("id") or goal.get("goal_id") or "").strip()
            if gid:
                out[gid] = goal
    return out


def _fetch_goals(client: Any, quest_id: str) -> Dict[str, Any]:
    list_goals = getattr(client, "list_quest_goals", None)
    if not callable(list_goals):
        raise QuestGoalSyncError("this client cannot list quest goals")
    data = list_goals(quest_id) or {}
    if "period_groups" not in data:
        raise QuestGoalSyncError(f"goals for quest {quest_id} not found or inaccessible")
    return data


def _fetch_goal_updates(client: Any, quest_id: str, known: Dict[str, Dict[str, Any]],
                        limit_per_goal: int) -> Dict[str, List[Dict[str, Any]]]:
    """Best-effort: each known goal's recent updates, duck-typed like ``_fetch_goals`` above.

    Tries the bulk method first (one call for every goal), falls back to fanning the per-goal
    method out one call per goal, and returns {} -- never raises -- when neither exists or either
    fails. Updates are additive detail on top of the goal list; a fetch problem here (an older
    backend, a permission gap, one bad goal) must never cost the person their goals.
    """
    if limit_per_goal <= 0 or not known:
        return {}
    bulk = getattr(client, "list_quest_goal_updates", None)
    if callable(bulk):
        try:
            data = bulk(quest_id, limit_per_goal=limit_per_goal) or {}
            return {str(gid): list(ups or []) for gid, ups in data.items()}
        except Exception as e:  # noqa: BLE001 -- fall back to the per-goal method instead
            log.info("bulk goal-updates fetch failed for quest %s (%s); trying per-goal",
                     quest_id, e)
    per_goal = getattr(client, "list_goal_updates", None)
    if not callable(per_goal):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for gid in known:
        try:
            out[gid] = list(per_goal(gid, limit=limit_per_goal) or [])
        except Exception as e:  # noqa: BLE001 -- one goal's updates failing never blocks the rest
            log.info("goal-updates fetch failed for goal %s (%s)", gid, e)
    return out


# --- the three entry points --------------------------------------------------

def pull_quest_goals(client: Any, quest_id: str, folder: str,
                     *, filename: str = GOALS_FILE_NAME,
                     updates_per_goal: int = 3) -> GoalSyncResult:
    """Quest -> local: (re)render the folder's goals file from the quest's goal ladder.

    Idempotent: pulling unchanged goals leaves the file byte-identical. Prose outside the managed
    markers is preserved. ``updates_per_goal`` caps how many of each goal's most recent updates
    render under its bullet (0 renders descriptions only) -- see ``render_goals_block``.
    """
    goal_data = _fetch_goals(client, quest_id)
    known = _known_goals(goal_data)
    updates_by_goal = _fetch_goal_updates(client, quest_id, known, updates_per_goal)
    path = _goals_path(folder, filename)
    existing = _read(path)
    rendered = render_goals_file(existing, quest_id, goal_data,
                                 updates_by_goal=updates_by_goal, updates_per_goal=updates_per_goal)
    if rendered != existing:
        _write(path, rendered)
    count = len(known)
    log.info("pulled %d goal(s) for quest %s -> %s", count, quest_id, path)
    return GoalSyncResult(direction="pull", quest_id=quest_id, goals_path=str(path),
                          pulled=True, goals_rendered=count)


def push_goals_to_quest(client: Any, quest_id: str, folder: str,
                        *, filename: str = GOALS_FILE_NAME) -> GoalSyncResult:
    """Local -> Quest: apply ticks, renames and new bullets found in the goals file.

    A created goal's bullet is rewritten in place with the id it was assigned, so re-running a
    push only ever sends what changed since the last one.
    """
    path = _goals_path(folder, filename)
    if not path.exists():
        raise QuestGoalSyncError(f"no goals file to push from at {path} — pull first")
    text = _read(path)
    goal_data = _fetch_goals(client, quest_id)
    edits = parse_goal_edits(text, _known_goals(goal_data))
    result = GoalSyncResult(direction="push", quest_id=quest_id, goals_path=str(path), pushed=True)
    if edits.is_empty():
        log.info("no local goal edits to push for quest %s", quest_id)
        return result

    complete_goal = getattr(client, "set_goal_completed", None)
    for gid in edits.completed:
        if not callable(complete_goal):
            log.warning("client cannot complete goals; %s left open", gid)
            break
        try:
            complete_goal(gid, completed=True)
            result.completed.append(gid)
        except Exception as e:  # noqa: BLE001 -- one failed edit never blocks the others
            log.warning("could not complete goal %s: %s", gid, e)

    update_goal = getattr(client, "update_goal", None)
    for gid, title in edits.renamed:
        if not callable(update_goal):
            log.warning("client cannot update goals; %s not renamed", gid)
            break
        try:
            update_goal(gid, {"title": title, "name": title})
            result.renamed.append(gid)
        except Exception as e:  # noqa: BLE001
            log.warning("could not rename goal %s: %s", gid, e)

    create_goal = getattr(client, "create_goal", None)
    new_ids: Dict[str, str] = {}
    for spec in edits.created:
        if not callable(create_goal):
            log.warning("client cannot create goals; %r not created", spec["title"])
            break
        try:
            created = create_goal(spec["title"], period=spec["period"], quest_id=quest_id) or {}
            gid = str(created.get("id") or created.get("goal_id") or "").strip()
            if gid:
                new_ids[spec["title"]] = gid
                result.created.append(gid)
            else:
                # No id back means we cannot mark the bullet, and an unmarked bullet is re-created
                # on every future push. Say so rather than counting it as sent.
                log.warning("created goal %r came back with no id; bullet left unmarked",
                            spec["title"])
        except Exception as e:  # noqa: BLE001
            log.warning("could not create goal %r: %s", spec["title"], e)

    if new_ids:
        _write(path, _stamp_new_ids(text, new_ids))
    log.info("pushed goal edits for quest %s: %d completed, %d renamed, %d created",
             quest_id, len(result.completed), len(result.renamed), len(result.created))
    return result


def _stamp_new_ids(text: str, new_ids: Dict[str, str]) -> str:
    """Rewrite each just-created bullet with the id it was assigned."""
    out: List[str] = []
    for raw in text.splitlines():
        m = _GOAL_LINE_RE.match(raw.strip())
        if m and not m.group("id"):
            title, due = _split_due(m.group("text") or "")
            gid = new_ids.get(title)
            if gid:
                indent = raw[:len(raw) - len(raw.lstrip())]
                suffix = f" (due {due})" if due else ""
                out.append(f"{indent}- [{m.group('box')}] <!-- id:{gid} --> {title}{suffix}")
                continue
        out.append(raw)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def sync_quest_goals(client: Any, quest_id: str, folder: str, direction: str = "pull",
                     *, filename: str = GOALS_FILE_NAME,
                     updates_per_goal: int = 3) -> GoalSyncResult:
    """The one entry point.

    ``"both"`` pushes BEFORE it pulls, unlike ``quest_folder_sync``. Local goal edits live inside
    the managed block, and a pull regenerates that block from Quest, so pulling first would erase
    a tick before it was ever sent. ``updates_per_goal`` is pull-only (see ``pull_quest_goals``)
    and defaults the same way a caller with no config object gets, so an existing call site that
    never passes it keeps working unchanged.
    """
    direction = (direction or "pull").lower()
    if direction == "pull":
        return pull_quest_goals(client, quest_id, folder, filename=filename,
                                updates_per_goal=updates_per_goal)
    if direction == "push":
        return push_goals_to_quest(client, quest_id, folder, filename=filename)
    if direction == "both":
        pushed = push_goals_to_quest(client, quest_id, folder, filename=filename)
        pulled = pull_quest_goals(client, quest_id, folder, filename=filename,
                                  updates_per_goal=updates_per_goal)
        return GoalSyncResult(
            direction="both", quest_id=quest_id, goals_path=pulled.goals_path,
            pulled=True, pushed=True, goals_rendered=pulled.goals_rendered,
            completed=pushed.completed, renamed=pushed.renamed, created=pushed.created,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
