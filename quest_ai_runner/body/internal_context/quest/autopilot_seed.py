"""Seed autopilot settings from a file without ever overwriting what a person set in Quest.

A script or pass that holds autopilot settings in files (a quest's prompts, a roster) hands them here instead of calling
the client's write methods. Each guarded field is written only when

* Quest holds nothing for it (unset, empty), or
* Quest still holds exactly what this ledger says the runner itself last wrote there (nobody changed it since), or
* the value already equals the file's (nothing to write; the ledger just learns it), or
* ``force`` is true (a person at a terminal said so).

Any other field is a CONFLICT: a person (or another session) changed it in Quest. It is reported, in the result and as one log
line, and left as it is. The ledger is a JSON file of SHA-256 hashes (never the text) of the values the runner wrote, keyed by
quest and autopilot, so it holds no content and is safe beside the script. It is the same idea as the goal ownership guard: the
runner writes only what is still its own.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.autopilot_seed")

# Every setting a person can change on a named autopilot; the rule covers all of them.
GUARDED_FIELDS = ("name", "purpose", "model", "cadence", "days", "run_time", "mode", "planning", "instructions", "rep_id",
                  "enabled", "email.recipients")
# The quest-level fields the legacy quest route still takes.
QUEST_LEVEL_FIELDS = ("instructions", "run_timezone", "env_id", "email.recipients")
QUEST_LEVEL_KEY = "@quest"


def _get(settings: Dict[str, Any], key: str) -> Any:
    head, _, tail = key.partition(".")
    value = (settings or {}).get(head)
    return _get(value, tail) if tail and isinstance(value, dict) else (None if tail else value)


def _has(settings: Dict[str, Any], key: str) -> bool:
    head, _, tail = key.partition(".")
    return head in (settings or {}) and (not tail or (isinstance(settings[head], dict) and _has(settings[head], tail)))


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _body(changes: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    for key, value in changes.items():
        head, _, tail = key.partition(".")
        if tail:
            body.setdefault(head, {})[tail] = value
        else:
            body[head] = value
    return body


@dataclass
class SeedReport:
    """What one seed did: ``written`` and ``unchanged`` field names, and ``conflicts`` (field, live value kept, file value not applied)."""
    target: str
    written: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts


class SeedLedger:
    """The hashes of what the runner last wrote, one JSON file. ``None`` as the path keeps it in memory only (nothing is then ever 'ours')."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path).expanduser() if path else None
        try:
            self.data: Dict[str, Dict[str, str]] = json.loads(self.path.read_text()) if self.path and self.path.exists() else {}
        except (OSError, ValueError):
            self.data = {}

    def last(self, target: str, key: str) -> Optional[str]:
        return (self.data.get(target) or {}).get(key)

    def remember(self, target: str, key: str, value: Any) -> None:
        self.data.setdefault(target, {})[key] = digest(value)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))


def plan_seed(target: str, live: Dict[str, Any], wanted: Dict[str, Any], ledger: SeedLedger, fields: tuple, force: bool = False):
    """``(changes, report)``: the dotted-field values to write and the report. Pure; nothing is written."""
    report, changes = SeedReport(target), {}
    for key in fields:
        want = _get(wanted, key)
        if not _has(wanted, key):
            continue
        have = _get(live, key)
        if have == want:
            report.unchanged.append(key)
        elif _empty(have) or force or ledger.last(target, key) == digest(have):
            changes[key] = want
        else:
            report.conflicts.append({"field": key, "kept": have, "file": want})
            log.warning("autopilot seed %s: %s differs in Quest from the file and was changed there since the runner last wrote it; kept Quest's value", target, key)
    return changes, report


def _seed(target, live, wanted, ledger, fields, force, write, reread):
    changes, report = plan_seed(target, live, wanted, ledger, fields, force)
    if changes:
        write(_body(changes))
        after = reread() or {}
        report.written = list(changes)
        for key, value in changes.items():
            ledger.remember(target, key, _get(after, key) if not _empty(_get(after, key)) else value)
    for key in report.unchanged:
        ledger.remember(target, key, _get(live, key))
    ledger.save()
    return report


def seed_autopilot(client, quest_id: str, autopilot_id: str, wanted: Dict[str, Any], ledger: SeedLedger, *, force: bool = False) -> SeedReport:
    """Seed one named autopilot from ``wanted`` (field name to value; email recipients as ``{"email": {"recipients": [...]}}``). Raises ``KeyError`` when Quest has no such autopilot."""
    def live_now():
        stored = (client.list_autopilots(quest_id) or {}).get("autopilots") or []
        return next((a for a in stored if a.get("id") == autopilot_id), None)
    live = live_now()
    if live is None:
        raise KeyError(f"{quest_id} has no autopilot {autopilot_id}")
    return _seed(f"{quest_id}/{autopilot_id}", live, wanted, ledger, GUARDED_FIELDS, force,
                 lambda body: client.update_autopilot(quest_id, autopilot_id, body), live_now)


def seed_quest_autopilot(client, quest_id: str, wanted: Dict[str, Any], ledger: SeedLedger, *, force: bool = False) -> SeedReport:
    """Seed the quest-level fields (instructions, run_timezone, env_id) the same way."""
    def live_now():
        return client.get_quest_autopilot(quest_id).get("autopilot") or {}
    return _seed(f"{quest_id}/{QUEST_LEVEL_KEY}", live_now(), wanted, ledger, QUEST_LEVEL_FIELDS, force,
                 lambda body: client.update_quest_autopilot(quest_id, body), live_now)
