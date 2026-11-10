"""Where things are, for a deep worker that starts cold: the files earlier runs on the same goal used, and a map of the folder.

A worker launched in a working folder used to spend its first turns on ``ls``, ``grep`` and ``cat`` to find what the system already
knew or an earlier run already found. This module closes that from both ends, with no model call:

  * after a run, ``record_run_trail`` keeps the files that run read and changed (``hubofhubs`` ``TrailStore``: the newest five runs
    per working folder and goal, files that runs changed first, then by how many runs touched them);
  * before a run, ``where_things_are`` renders those files (the ones that still exist) and a folder map as one prompt block.

Both are best-effort by contract: a failure to record or to render is logged and the run goes on without the block.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Union

from hubofhubs.api import session as hub

from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import folder_map, folder_summary
from quest_ai_runner.the_intelligence.mind.delegation.goal_runner_session import resolve_session_file

log = logging.getLogger("quest-ai-runner.exploration_trail")

TRAIL_HEADER = "--- WHERE THINGS ARE ---"
TRAIL_FILES_LISTED = 12
BLOCK_MAX_CHARS = 4000
UNKEYED_GOAL = "folder"   # a run with no goal of its own shares one trail per working folder


def trail_directory() -> str:
    """Where trails are kept: ``QAR_TRAIL_DIR``, else ``~/.quest-ai-runner/trails`` (next to the other per-user state)."""
    return os.environ.get("QAR_TRAIL_DIR") or str(Path.home() / ".quest-ai-runner" / "trails")


def goal_key_of(ctx_meta: Optional[dict]) -> str:
    """The widest key a run's trail is kept under: the quest it serves, else the working folder alone."""
    return str((ctx_meta or {}).get("quest_id") or UNKEYED_GOAL)


def scope_keys(ctx_meta: Optional[dict], solution_ids: Sequence[str] = ()) -> List[str]:
    """The keys a run's trail is kept under and read from, narrowest first: each solution the run works on (a solution is the scope of focused
    progress, so what one run on it found is what the next run on it needs), then the quest, which catches work that names no solution."""
    keys = [f"solution:{sid}" for sid in dict.fromkeys(str(s).strip() for s in solution_ids) if sid]
    return keys + [goal_key_of(ctx_meta)]


def record_run_trail(working_dir: Optional[str], goal_key: Union[str, Sequence[str]], session_id: Optional[str], store: Any = None) -> bool:
    """Keep the files this run read and changed, read from its session record. Never raises; True when something was kept."""
    try:
        if not working_dir or not session_id:
            return False
        path = resolve_session_file(working_dir, session_id)
        if path is None:
            return False
        trail = hub.run_trail(hub.exploration_account(path), working_dir)
        kept = store or hub.TrailStore(trail_directory())
        return any([kept.record(working_dir, key, trail) for key in ([goal_key] if isinstance(goal_key, str) else list(goal_key))])
    except Exception as e:  # noqa: BLE001 - recording must never fail the run
        log.warning("could not record the trail of a run in %s: %s", working_dir, e)
        return False


def where_things_are(working_dir: Optional[str], goal_key: Union[str, Sequence[str]], store: Any = None) -> str:
    """The "where things are" block for a deep prompt, or "" without a readable working folder. Names only, never file contents."""
    try:
        if not working_dir or not os.path.isdir(working_dir):
            return ""
        kept = store or hub.TrailStore(trail_directory())
        keys = [goal_key] if isinstance(goal_key, str) else list(goal_key)
        ranked, listed = [], set()
        for key in keys:        # narrowest scope first: the solution's own files lead, the quest's fill what is left
            for r in kept.ranked(working_dir, key, limit=TRAIL_FILES_LISTED * 3):
                if r["path"] not in listed:
                    listed.add(r["path"])
                    ranked.append(r)
        scoped = any(str(k).startswith("solution:") for k in keys)
        used: List[str] = [f"- {r['path']}" + (" (changed)" if r.get("changed") else "")
                           for r in ranked if os.path.isfile(os.path.join(working_dir, r["path"]))][:TRAIL_FILES_LISTED]
        tree = folder_map(working_dir)
        summary = folder_summary(working_dir)
        parts = [TRAIL_HEADER]
        if used:
            parts.append(("Files earlier runs on this solution used" if scoped else "Files earlier runs on this work used") + " (open these first, do not search for them):\n" + "\n".join(used))
        if tree or summary:
            parts.append("Folder map" + (f" ({summary})" if summary else "") + ":\n" + "\n".join(tree))
        if len(parts) == 1:
            return ""
        parts.append("Start from these. Search only for what is not listed here.")
        return "\n\n".join(parts)[:BLOCK_MAX_CHARS]
    except Exception as e:  # noqa: BLE001 - a missing block must never stop a run
        log.warning("could not build the where-things-are block for %s: %s", working_dir, e)
        return ""
