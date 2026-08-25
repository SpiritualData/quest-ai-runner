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
checkbox bullets carrying their id::

    <!-- QAR:MANAGED:goals START -->
    ## Goals

    ### Quarter
    **Q3 2026 (Jul - Sep)** <!-- period:2026_Q3 scope:quarter -->
    - [ ] <!-- id:goal_00c5922b --> Secure commitment from all committee members (due 2026-09-30)
    - [x] <!-- id:goal_bfeda075 --> Finish the concept paper
    <!-- QAR:MANAGED:goals END -->

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


def render_goals_block(goal_data: Dict[str, Any]) -> str:
    """The managed block: every goal, grouped by period, ids carried inline.

    Completed goals are kept and ticked rather than dropped. A plan that silently loses its
    finished rows reads, a quarter later, as though the work was never scheduled -- and it would
    also make the file disagree with Quest about what exists, which is what breaks push.
    """
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
        lines.append("")
    return "\n".join(lines).rstrip()


def _ensure_frontmatter(existing: str, quest_id: str) -> str:
    if _FRONTMATTER_RE.match(existing or ""):
        return existing
    return f"---\nquest_id: {quest_id}\n---\n\n{existing or ''}"


def render_goals_file(existing: str, quest_id: str, goal_data: Dict[str, Any]) -> str:
    """Render the managed block into the goals file, preserving everything else."""
    out = _ensure_frontmatter(existing or "", quest_id)
    return replace_between(out, _GOALS_START, _GOALS_END, render_goals_block(goal_data))


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


# --- the three entry points --------------------------------------------------

def pull_quest_goals(client: Any, quest_id: str, folder: str,
                     *, filename: str = GOALS_FILE_NAME) -> GoalSyncResult:
    """Quest -> local: (re)render the folder's goals file from the quest's goal ladder.

    Idempotent: pulling unchanged goals leaves the file byte-identical. Prose outside the managed
    markers is preserved.
    """
    goal_data = _fetch_goals(client, quest_id)
    path = _goals_path(folder, filename)
    existing = _read(path)
    rendered = render_goals_file(existing, quest_id, goal_data)
    if rendered != existing:
        _write(path, rendered)
    count = len(_known_goals(goal_data))
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
                     *, filename: str = GOALS_FILE_NAME) -> GoalSyncResult:
    """The one entry point.

    ``"both"`` pushes BEFORE it pulls, unlike ``quest_folder_sync``. Local goal edits live inside
    the managed block, and a pull regenerates that block from Quest, so pulling first would erase
    a tick before it was ever sent.
    """
    direction = (direction or "pull").lower()
    if direction == "pull":
        return pull_quest_goals(client, quest_id, folder, filename=filename)
    if direction == "push":
        return push_goals_to_quest(client, quest_id, folder, filename=filename)
    if direction == "both":
        pushed = push_goals_to_quest(client, quest_id, folder, filename=filename)
        pulled = pull_quest_goals(client, quest_id, folder, filename=filename)
        return GoalSyncResult(
            direction="both", quest_id=quest_id, goals_path=pulled.goals_path,
            pulled=True, pushed=True, goals_rendered=pulled.goals_rendered,
            completed=pushed.completed, renamed=pushed.renamed, created=pushed.created,
        )
    raise ValueError(f"unknown sync direction {direction!r}; use 'pull', 'push', or 'both'")
