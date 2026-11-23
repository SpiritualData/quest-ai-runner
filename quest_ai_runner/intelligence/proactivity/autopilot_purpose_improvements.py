"""The ``quest_improvements`` work source: keep every improvement idea people send to Quest moving, and tell them what became of it.

Quest AI is the rep that answers for the app itself across all users. This source is the autopilot half: before any model is asked, one cheap
read (``GET /api/assistant-tasks/suggestions/follow-up``) says what is unacted: ideas waiting for a decision, approved ideas not yet worked, and
resolved ideas whose suggester has not been told. An idea awaiting review is listed once: the pass notes it (``/noted``), since after the
recommendation it waits on a person. The telling is deterministic (the backend mails the suggester as Quest AI with the team
groups on BCC, once each) and runs here with no model. Only what still needs judgment is listed as due; when nothing is, the run is refused
and closed as skipped, so a quiet day spends no tokens.

What is due gets a run: ideas awaiting review get a recommendation and an ask for the Spiritual Data reviewer (approving stays a person's call);
approved ideas not yet worked, or stalled, are implemented by the run itself. The run ends with a SUGGESTION STATUS block, and
``report_statuses`` (called by the executor when the run ends) posts each line to ``/api/assistant-tasks/suggestions/{id}/status``, which updates
the idea and emails its suggester.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import log
from quest_ai_runner.intelligence.proactivity.autopilot_sources import WorkSource, register_source

PATH = "/api/assistant-tasks/suggestions/follow-up"
SOURCE_NAME = "quest_improvements"

SCOPE = ("keep every improvement idea sent through Improve Quest moving: no idea waits for a decision unnoticed, no approved idea stalls, "
         "and each suggester is told what became of their idea.")

RULES = """- The items listed below are the whole answer to what is unacted. Look at nothing else.
- An idea awaiting review: say in ONE add_quest_note line per idea whether to approve or reject it and why (user retention first), then raise ONE create_decision for the Spiritual Data reviewer with that recommendation. Never approve or reject it yourself: that is a person's review.
- An approved idea not yet worked, or one whose work stalled: you implement it. Make the change at its general cause on the current month branch of the repo it belongs to (quest-frontend, quest-backend or quest-ai-runner), follow that repo's playbook, test it (in the browser for anything a user sees), commit only your files, never push or release.
- Report each approved idea you worked in the SUGGESTION STATUS block at the very end of your output, one line per idea: `<task id> | done | <how it was applied, in two plain sentences the person who suggested it understands>` or `<task id> | failed | <what stopped it>`. The runner sends these: the idea's status is updated and Quest AI emails the suggester, with the team groups on BCC. Never write to a user yourself.
- If an idea needs a person (a taste call, payment, anything irreversible), do not report it; raise ONE create_decision naming what is needed and from whom.
- Post ONE short note on the quest at the end: how many ideas you looked at, which you applied, and what you asked a person to decide.

SUGGESTION STATUS
<task id> | done | <how>
END SUGGESTION STATUS"""


def fetch_state(client: Any) -> Optional[Mapping[str, Any]]:
    """The follow-up state, or None when the read failed (logged, never silent)."""
    try:
        return client.send_request("GET", PATH)
    except Exception as error:  # noqa: BLE001 -- a failed read must not abort the pass
        log.warning("quest_improvements: could not read the follow-up state: %s", error)
        return None


def tell_suggesters(client: Any) -> None:
    """Have the backend mail every resolved idea's suggester, once each. No model."""
    try:
        client.send_request("POST", PATH, body={})
    except Exception as error:  # noqa: BLE001
        log.warning("quest_improvements: telling suggesters failed: %s", error)


def improvements_due(client: Any) -> List[Mapping[str, Any]]:
    """Ideas needing judgment now (awaiting review, approved and not worked). Tells suggesters first when any are owed a mail."""
    state = fetch_state(client)
    if not state or not state.get("total"):
        return []
    if state.get("to_tell"):
        tell_suggesters(client)
    due = [item for item in state.get("items") or [] if item.get("kind") != "tell_suggester"]
    note_awaiting(client, [str(item.get("task_id")) for item in due if item.get("kind") == "awaiting_review"])
    return [{"id": str(item.get("task_id")), "what": f"{item.get('what') or item.get('title') or ''} (task id {item.get('task_id')})"}
            for item in due]


def note_awaiting(client: Any, task_ids: List[str]) -> None:
    """Tell Quest these ideas awaiting review are taken into this run, so the next state read lists each once, not every day while it waits
    on a person (its review ask was already raised when it was filed). No model."""
    if not task_ids:
        return
    try:
        client.send_request("POST", f"{PATH}/noted", body={"task_ids": task_ids})
    except Exception as error:  # noqa: BLE001 -- a failed stamp only means the idea is listed again next pass
        log.warning("quest_improvements: could not note ideas awaiting review: %s", error)


def improvements_purpose(client: Any) -> WorkSource:
    def due_items(now: datetime) -> Sequence[Mapping[str, Any]]:
        return improvements_due(client)
    return WorkSource(SOURCE_NAME, SCOPE, RULES, due_items)


STATUS_START, STATUS_END = "SUGGESTION STATUS", "END SUGGESTION STATUS"
_STATUS_BLOCK = re.compile(rf"^[ \t>*#`-]*{STATUS_START}[ \t*`:]*\n(.*?)\n[ \t>*#`-]*{STATUS_END}", re.S | re.M)
_STATUS_LINE = re.compile(r"^[\s>*`-]*([A-Za-z0-9_-]{4,})\s*\|\s*(done|failed)\s*\|\s*(.+?)\s*`?$", re.I)


def parse_statuses(output: str) -> List[Dict[str, str]]:
    """The approved ideas the run reports as done or failed, from the LAST SUGGESTION STATUS block: ``[{task_id, status, how}]``. The brief's own
    template line (``<task id> | ...``) never parses, so an echoed example sends nothing."""
    blocks = _STATUS_BLOCK.findall(output or "")
    if not blocks:
        return []
    out: List[Dict[str, str]] = []
    for line in blocks[-1].splitlines():
        match = _STATUS_LINE.match(line.strip())
        if match:
            out.append({"task_id": match.group(1), "status": match.group(2).lower(), "how": match.group(3).strip()})
    return out


def report_statuses(client: Any, output: str) -> int:
    """Send each reported idea's status to Quest, which updates it and emails the suggester once. Returns how many were taken; never raises."""
    taken = 0
    for item in parse_statuses(output):
        try:
            client.send_request("POST", f"/api/assistant-tasks/suggestions/{item['task_id']}/status", body={"status": item["status"], "how": item["how"]})
            taken += 1
        except Exception as error:  # noqa: BLE001 -- a finished run stays finished
            log.warning("quest_improvements: Quest refused the status of idea %s: %s", item["task_id"], error)
    return taken


def register_for(client: Any) -> WorkSource:
    """Register the source bound to this lane's Quest client (the autopilot pass does this once at construction)."""
    return register_source(improvements_purpose(client))
