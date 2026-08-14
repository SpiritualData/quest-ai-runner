"""session_next_steps — the ATTENDED half of a quest folder's canonical next-steps artifact.

``quest_folder_sync`` gives a quest folder one standing answer to "what do I do next here"
(the ``QAR:MANAGED:next_steps`` block in ``QUEST_SYNC.md``), and the autopilot pass reads it and
refreshes it. An attended ``chat`` session in that same folder did neither: it was free to
re-derive the answer from goals, notes and files every time it was asked, while a considered answer
sat in the folder it was standing in. Retrieval could surface the file like any other, but only by
competing with the whole corpus on relevance, which is not what a standing answer is for.

This module is the shared, UI-free half of closing that gap:

    standing = load_standing_next_steps(cfg)            # session start: read it, once
    block    = render_standing_next_steps(standing)     # every turn: authoritative context
    result   = refresh_from_turn(client, standing, goals=..., deep_results=..., updated=...)

``InteractiveSession`` (the session brain every chat entry point constructs, whatever renders it) is
the only caller, so no UI carries a copy of this logic and the trigger cannot drift between them.

Everything here is best-effort by contract and returns None rather than raising: no sync file, no
next-steps block, no quest mapping, or a Quest API failure on the write must all leave an attended
session behaving exactly as it did before this existed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Tuple

from .quest_folder_sync import (
    SYNC_FILE_NAME,
    NextSteps,
    NextStepsResult,
    publish_next_steps,
    quest_for_path,
    quest_id_in_folder,
    read_next_steps,
    render_next_steps,
)

log = logging.getLogger("quest-ai-runner.session_next_steps")

# What we are willing to prepend to EVERY turn. The block is normally a handful of lines, but it is
# a plain markdown section a human may edit by hand, and an unbounded one would silently tax every
# call in the session. The same ceiling the Quest-side write uses, for one obvious number.
STANDING_NEXT_STEPS_MAX_CHARS = 4000

# Named as the session's own conclusion so a later reader (the next pass, the next session, the
# person) can tell at a glance which writer last touched the artifact.
ATTENDED_SOURCE = "an attended chat session"


@dataclass
class StandingNextSteps:
    """The next-steps artifact found in the folder this session is standing in.

    ``text`` is the block's body exactly as written, because whoever refreshed it last may have been
    a human editing it by hand (see ``read_next_steps``). ``quest_id`` may be empty: the artifact is
    perfectly readable without knowing which quest it belongs to, and only the write-back needs an
    id, so a folder that cannot be mapped still contributes its answer to every turn.
    """
    folder: str
    text: str
    quest_id: str = ""

    def can_refresh(self) -> bool:
        return bool(self.quest_id and self.folder)


def resolve_quest_folder(cfg: Any) -> Tuple[str, str]:
    """``(quest_id, folder)`` for the place this session is standing in. Either may be "".

    The deployment's own ``quest_folder_map`` answers FIRST, through the same ``quest_for_path``
    reverse lookup the autopilot pass uses, so both halves agree on which quest a folder is. That
    lookup also returns the MAPPED folder rather than the session's root, which matters when a
    session starts in a subfolder of the quest's folder: the artifact lives at the mapped root.

    When no map is configured (it is opt-in env config, and a chat user need not have set it up),
    the folder's own sync-file frontmatter is the fallback answer. Any folder that was ever pulled
    or given a next-steps artifact carries ``quest_id:`` there, so the common case still resolves
    with nothing to configure. Never raises: an unreadable path means "no quest here".
    """
    folder = ""
    quest_id = ""
    try:
        root = getattr(cfg, "corpus_root", None) or os.getcwd()
    except OSError:  # pragma: no cover - a deleted cwd
        return "", ""
    try:
        mapped = quest_for_path(getattr(cfg, "quest_folder_map", None) or {}, root)
    except Exception:  # noqa: BLE001 - resolution is best-effort at a chat startup
        mapped = None
    if mapped:
        quest_id, folder = mapped
    else:
        folder = str(root)
        try:
            quest_id = quest_id_in_folder(folder) or ""
        except Exception:  # noqa: BLE001 - an unreadable sync file is "no id", not a crash
            quest_id = ""
    return quest_id, folder


def load_standing_next_steps(cfg: Any) -> Optional[StandingNextSteps]:
    """Read the standing next-steps artifact for this session's folder, or None.

    A pure local file read: no Quest call, nothing to wait on, so it is safe in the session
    constructor. None means "behave exactly as before" and covers every degraded case (no folder,
    no sync file, no next-steps block, an empty block, an unreadable file).
    """
    quest_id, folder = resolve_quest_folder(cfg)
    if not folder:
        return None
    try:
        text = read_next_steps(folder)
    except Exception:  # noqa: BLE001 - the artifact is never worth failing a session start
        log.info("could not read the next-steps artifact in %s", folder, exc_info=True)
        return None
    if not text:
        return None
    return StandingNextSteps(folder=str(folder), text=text, quest_id=quest_id)


def render_standing_next_steps(standing: Optional[StandingNextSteps]) -> str:
    """The artifact as authoritative background context for a turn, or "" when there is none.

    Labelled, not pasted bare: the model has to be able to tell this apart from a card retrieval
    happened to surface, because the two deserve different treatment. A retrieved file is evidence;
    this is the current answer to the very question a person standing in this folder is most likely
    to ask, and the whole point of the artifact is that it is not re-derived per session.

    The freshness stamp travels INSIDE the block (``render_next_steps`` writes "Refreshed <date>, by
    <source>" as its first line), so this wrapper does not restate it and cannot contradict it.
    """
    if standing is None or not standing.text.strip():
        return ""
    body = standing.text.strip()
    if len(body) > STANDING_NEXT_STEPS_MAX_CHARS:
        body = (body[:STANDING_NEXT_STEPS_MAX_CHARS].rstrip()
                + f"\n\n(truncated; the full block is in {standing.folder}/{SYNC_FILE_NAME})")
    return (
        f"This quest's folder carries a STANDING NEXT-STEPS ARTIFACT ({SYNC_FILE_NAME}, kept "
        "current by the background pass and by attended sessions). It is the current authoritative "
        "answer to \"what should I do next here\", not one search result among many. When the "
        "question is what to do next, or what the state of this work is, START from it and say "
        "where it came from, instead of re-deriving an answer from scratch. Its own first line says "
        "when it was last refreshed and by whom, so judge its age yourself: if what you can see has "
        "clearly moved past it, say so plainly rather than repeating it.\n\n"
        f"{body}"
    )


def next_steps_from_turn(goals: Sequence[str], deep_results: Sequence[Any], *,
                         kind: str = "deep", updated: str = "") -> Optional[NextSteps]:
    """This turn's conclusion about what is still next, or None when it justifies no refresh.

    Deterministic and LLM-free, exactly like ``autopilot.next_steps_from_pass``: the turn has
    already decided and acted, so the artifact restates what it left unfinished rather than paying
    for a second opinion about it.

    THE WHOLE TRIGGER LIVES HERE, in the half neither UI owns, so a turn refreshes the artifact on
    identical terms wherever it ran and the decision can be read in one place.

    None (leave the standing answer alone) is returned for everything except a turn that ran real
    work and did not finish all of it:

    * A turn that is not a completed ``deep`` result: an answer, a confirm, a cancelled turn. The
      ``kind`` comes from the orchestrator's own structured result, never from reading its prose.
    * No goals or no deep results: nothing executed, so nothing about "what is next" changed. This
      is every ordinary answer turn, every clarifying question, every plan that was not run.
    * Every goal finished cleanly: the turn knows what it COMPLETED but not what comes after it, and
      deciding that is a planning judgment this deliberately does not spend a model call on.
      Replacing a considered answer with an empty block would leave the folder worse off than the
      stale one it overwrote, the same reason a quiet autopilot pass leaves the artifact alone; the
      next pass has the goal rows to write a real answer from.

    A DEFERRED result counts as unfinished. It means the work was handed off to run out of band, so
    the goal is queued rather than done, and an artifact that reported it as complete would be
    claiming an outcome nobody has seen yet.

    Attribution is per goal only when the two lists line up. A sequential-group deep run records
    results without a matching goal entry, so lengths can differ; in that case the only honest read
    is all-or-nothing, and a partially finished run keeps every goal listed rather than guessing
    which one the unfinished result belonged to.
    """
    if (kind or "") != "deep":
        return None
    goal_list = [str(g).strip() for g in (goals or []) if str(g).strip()]
    results = list(deep_results or [])
    if not goal_list or not results:
        return None

    def _finished(result: Any) -> bool:
        return bool(getattr(result, "met", False)) and not bool(getattr(result, "deferred", False))

    if len(goal_list) == len(results):
        remaining = [g for g, r in zip(goal_list, results) if not _finished(r)]
    else:
        remaining = [] if all(_finished(r) for r in results) else list(goal_list)
    if not remaining:
        return None

    done = sum(1 for r in results if _finished(r))
    note = (f"Left unfinished by {ATTENDED_SOURCE}"
            f"{f' on {updated}' if updated else ''}: it worked {len(results)} "
            f"goal{'s' if len(results) != 1 else ''} and {done} finished cleanly.")
    return NextSteps(steps=remaining, source=ATTENDED_SOURCE, updated=updated, note=note)


def refresh_from_turn(client: Any, standing: Optional[StandingNextSteps], *,
                      goals: Sequence[str], deep_results: Sequence[Any],
                      kind: str = "deep", updated: str = "") -> Optional[NextStepsResult]:
    """Publish this turn's conclusion as the quest's standing answer, when it warrants one.

    Returns None when nothing was written, for any reason: no artifact/folder resolved, no quest id
    to publish under, the turn did not justify a refresh (see :func:`next_steps_from_turn`), or the
    write failed. ``client`` may be None — the local file is still refreshed, since it is the copy
    the person is working next to, and ``publish_next_steps`` reports the missing Quest side in its
    ``detail`` rather than raising.

    On success ``standing.text`` is updated in place, so the REST of the session sees the answer it
    just wrote instead of continuing to assert the one it replaced.
    """
    if standing is None or not standing.can_refresh():
        return None
    next_steps = next_steps_from_turn(goals, deep_results, kind=kind, updated=updated or _today())
    if next_steps is None:
        return None
    try:
        result = publish_next_steps(client, standing.quest_id, standing.folder, next_steps)
    except Exception:  # noqa: BLE001 - the artifact must never fail an otherwise-good turn
        log.warning("could not refresh the next-steps artifact for quest %s",
                    standing.quest_id, exc_info=True)
        return None
    standing.text = render_next_steps(next_steps)
    return result


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


__all__ = [
    "STANDING_NEXT_STEPS_MAX_CHARS",
    "ATTENDED_SOURCE",
    "StandingNextSteps",
    "resolve_quest_folder",
    "load_standing_next_steps",
    "render_standing_next_steps",
    "next_steps_from_turn",
    "refresh_from_turn",
]
