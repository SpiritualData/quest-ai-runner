"""Pending work a deployment names for a quest: a reason for the pass to run before cadence is due.

A quest's folder can carry a file listing work that has arrived and not yet been handled (for the
SD meetings flow, ``BACKLOG.md``: the Drive transcripts whose id is in no summary's front matter,
rewritten every few minutes by a cron with no AI). The lane's config names that file per quest
(``RunnerConfig.autopilot_pending_work``); nothing here knows which quest, which folder or what the
items are. The file is read instead of the source system (Drive) because the pass process has no
Drive client and the file's writer already computes exactly the pending set.

File contract: a Markdown table (header row, separator row, one row per pending item). Anything
else, including a sentence saying nothing is pending, counts as zero.

Pending work makes a quest due at most once per day (see ``pass_due``): the count is a reason to
look, not a licence to loop while the work waits to be done.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from .autopilot_defaults import log
from .autopilot_periods import cadence_due

_SEPARATOR_CELL = re.compile(r"^:?-{1,}:?$")


@dataclass(frozen=True)
class PendingWork:
    """What a quest's pending file held when the pass looked."""
    count: int
    label: str
    source: str


def count_pending_rows(text: str) -> int:
    """Rows of the first Markdown table in ``text`` that follow its separator row."""
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("|") or i == 0 or not lines[i - 1].startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(_SEPARATOR_CELL.match(c) for c in cells):
            rows = 0
            for row in lines[i + 1:]:
                if not row.startswith("|"):
                    break
                rows += 1
            return rows
    return 0


def pending_work_for(quest_id: str, specs: Optional[Dict[str, Any]],
                     folder_map: Optional[Dict[str, str]]) -> Optional[PendingWork]:
    """The quest's pending work, or ``None`` when none is configured or it could not be read.

    ``specs`` is ``{quest_id: {"file": path, "label": "what the items are"}}``; a relative ``file``
    is under the quest's folder in ``folder_map``. An unreadable file logs and returns ``None``:
    a read that failed is not evidence that anything is pending, and it must not run passes.
    """
    spec = (specs or {}).get(quest_id)
    if not isinstance(spec, dict) or not spec.get("file"):
        return None
    path = str(spec["file"])
    if not os.path.isabs(path):
        folder = (folder_map or {}).get(quest_id)
        if not folder:
            log.warning("autopilot: pending-work file %r for quest %s is relative but the quest "
                        "has no folder in quest_folder_map", path, quest_id)
            return None
        path = os.path.join(folder, path)
    try:
        with open(path, encoding="utf-8") as fh:
            count = count_pending_rows(fh.read())
    except OSError as e:
        log.warning("autopilot: could not read pending-work file %s for quest %s: %s",
                    path, quest_id, e)
        return None
    return PendingWork(count=count, label=str(spec.get("label") or "items awaiting work"),
                       source=path)


def pass_due(autopilot_cfg: Dict[str, Any], now: datetime, tz: Optional[str],
             pending: int = 0) -> bool:
    """``cadence_due``, except that pending work makes the quest due once per day at least.

    One predicate for the gate inside the pass and the schedule that decides which day to create
    the pass for, so the two cannot disagree (the same promise ``cadence_due`` already keeps).
    """
    if pending > 0:
        autopilot_cfg = {**autopilot_cfg, "cadence": "daily"}
    return cadence_due(autopilot_cfg, now, tz=tz)
