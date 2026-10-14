"""Collection state: watermarks, relayed items, the relay log and ``CollectRequest``.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Optional
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .context_update_types import _as_utc, _utcnow, log


# ---------------------------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------------------------

class Watermarks:
    """Per (card, source) "an assistant last looked at this" timestamps, JSON-backed.

    Deliberately per SOURCE and not one stamp per card. The channels move at different speeds and
    are consumed by different surfaces: an attended chat may read the quest's notes without ever
    touching the document comments, and a single card-wide stamp would then mark the comments as
    seen because something else was. Per source, each channel advances only when that channel was
    actually delivered.

    A missing file, an unreadable one and a card never seen before all read as "never looked",
    which the engine turns into the bounded first-look window rather than into everything or
    nothing.

    ``read_only`` is what makes LOOKING at a card's context a read. Collection never writes -- only
    ``ContextUpdates.mark_seen`` does -- but "never call that one method" is a convention, and a
    convention is not something a person reviewing a command can verify for themselves. A read-only
    store cannot move a stamp whichever method is called on it, so an inspection can be run as
    often as anyone likes without consuming somebody's comment on the way past.
    """

    def __init__(self, path: Optional[str] = None, *, read_only: bool = False) -> None:
        self._path = Path(path) if path else None
        self._read_only = bool(read_only)
        self._data: Dict[str, str] = {}
        # Per (card, source, key) "what it looked like the last time a run was handed it", for a
        # source that has to say WHAT changed rather than only that something did (a Notion row's
        # properties). Persisted in the same file and under the same rules as the stamps.
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    @property
    def read_only(self) -> bool:
        return self._read_only

    @staticmethod
    def _key(card_id: str, source: str) -> str:
        return f"{card_id}:{source}"

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text())
            seen = payload.get("last_seen") if isinstance(payload, dict) else None
            if isinstance(seen, dict):
                self._data = {str(k): str(v) for k, v in seen.items()}
            snaps = payload.get("snapshots") if isinstance(payload, dict) else None
            if isinstance(snaps, dict):
                self._snapshots = {str(k): v for k, v in snaps.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("context watermarks unreadable (%s); starting fresh", e)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            payload: Dict[str, Any] = {"last_seen": self._data}
            if self._snapshots:
                payload["snapshots"] = self._snapshots
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self._path)   # atomic: never a partial file after a crash
        except OSError as e:
            log.warning("could not persist context watermarks: %s", e)

    def get(self, card_id: str, source: str) -> Optional[datetime]:
        with self._lock:
            return _as_utc(self._data.get(self._key(card_id, source)))

    def set(self, card_id: str, source: str, when: datetime) -> None:
        if self._read_only:
            return
        with self._lock:
            key = self._key(card_id, source)
            existing = _as_utc(self._data.get(key))
            # Never move a watermark backwards: two lanes reading the same card would otherwise
            # re-deliver everything between them on every alternate run.
            if existing and existing >= when:
                return
            self._data[key] = when.astimezone(timezone.utc).isoformat()
            self._save()

    def get_snapshot(self, card_id: str, source: str, key: str) -> Optional[Dict[str, Any]]:
        """The snapshot a source stored for this (card, source, key), or None."""
        with self._lock:
            found = self._snapshots.get(f"{card_id}:{source}:{key}")
            return json.loads(json.dumps(found)) if isinstance(found, dict) else None

    def set_snapshot(self, card_id: str, source: str, key: str, snapshot: Dict[str, Any]) -> None:
        """Replace this (card, source, key) snapshot. A read-only store cannot."""
        if self._read_only or not isinstance(snapshot, dict):
            return
        with self._lock:
            self._snapshots[f"{card_id}:{source}:{key}"] = snapshot
            self._save()


class RelayedItems:
    """Which admitted items have already been posted onto Quest as a note, ever.

    A relay is not a read like the rest of delivery -- it is a WRITE with a real person's name on
    it, so unlike a watermark (which only says how far a read has gotten, and where a re-offered
    line costs nothing but a person's patience), losing this record turns a restart or a second
    pass over the same mail into a duplicate ask. Keyed by (card_id, source, item_id), the same
    triple the feedback ledger keys its own rows by (see ``docs/feedback-ledger.md``), because
    "have I done this already" and "what became of it" are the same identity asked two different
    questions -- kept as a SEPARATE store anyway, since the local feedback ledger is optional
    (``RunnerConfig.feedback_ledger``) and idempotent relay must hold regardless of whether a
    deployment keeps one.

    Same persistence discipline as ``Watermarks``: JSON-backed, atomic write (temp file + rename),
    best-effort (an unreadable file starts fresh rather than raising), and a ``read_only`` mode
    that cannot record a relay whichever method is called on it -- so a read-only engine (built for
    ``quest-ai-runner context <quest_id>``, or a preview) can never cause the very side effect it
    exists to avoid, even if something one day calls ``relay_admitted`` on it by mistake.
    """

    def __init__(self, path: Optional[str] = None, *, read_only: bool = False) -> None:
        self._path = Path(path) if path else None
        self._read_only = bool(read_only)
        self._data: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    @property
    def read_only(self) -> bool:
        return self._read_only

    @staticmethod
    def _key(card_id: str, source: str, item_id: str) -> str:
        return f"{card_id}:{source}:{item_id}"

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text())
            relayed = payload.get("relayed") if isinstance(payload, dict) else None
            if isinstance(relayed, dict):
                self._data = {str(k): str(v) for k, v in relayed.items()}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("relayed-items record unreadable (%s); starting fresh", e)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"relayed": self._data}, indent=2, sort_keys=True))
            os.replace(tmp, self._path)   # atomic: never a partial file after a crash
        except OSError as e:
            log.warning("could not persist relayed-items record: %s", e)

    def already_relayed(self, card_id: str, source: str, item_id: str) -> bool:
        with self._lock:
            return self._key(card_id, source, item_id) in self._data

    def mark_relayed(self, card_id: str, source: str, item_id: str, note_id: str = "") -> None:
        if self._read_only:
            return
        with self._lock:
            self._data[self._key(card_id, source, item_id)] = note_id or "1"
            self._save()


def relay_log_path_for(configured: Optional[str] = None,
                       state_path: Optional[str] = None) -> Optional[str]:
    """Where this deployment's relay record lives: what it configured, else beside its state file.

    Same derivation as ``watermark_path_for``/``feedback_ledger.ledger_path_for``, and for the same
    reason: without a path the record lives in memory for one process, and a restart would forget
    what it already posted, turning a re-offered mail item into a second note under the same
    person's name.
    """
    if configured:
        return configured
    if not state_path:
        return None
    p = Path(state_path)
    return str(p.with_name(p.stem + "_relayed_items.json"))


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------

@dataclass
class CollectRequest:
    """Everything a source needs to answer "what is new for this card".

    ``card`` is whatever the caller calls a unit of work (a quest row, a goal row, a task row).
    Sources read from it by key and tolerate absence; nothing here requires a particular schema,
    which is what lets the same source serve a quest-shaped caller and a card-shaped one.
    """
    card: Dict[str, Any] = field(default_factory=dict)
    card_id: str = ""
    card_kind: str = "quest"
    # The caller's own short label for this card. Passed through because the CALLER usually has a
    # better one than the card row does: an autopilot pass already resolved a display label, while
    # the quest state endpoint returns an outcome sentence and no name at all.
    card_label: str = ""
    spec: Dict[str, Any] = field(default_factory=dict)
    since: Optional[datetime] = None
    # Whether ``since`` is a real "a run was last handed this" stamp (False) or the bounded
    # first-look window for a card nothing has ever read (True). Sources that reason about what
    # happened SINCE a delivery need the difference: on a first look nothing has been delivered.
    first_look: bool = False
    now: datetime = field(default_factory=_utcnow)
    client: Any = None
    # Shared across every source ONE ``UpdateEngine`` invokes within ``CACHE_TTL_SECONDS`` (see
    # ``UpdateEngine._cache``), not per call: a user-scoped source (reflections, insights) reads
    # once and every subsequent card in the same pass narrows the same read in memory instead of
    # re-fetching it. The default factory only matters for a ``CollectRequest`` built standalone
    # (a test, or a source called outside the engine); ``UpdateEngine.collect`` always passes its
    # own dict explicitly.
    cache: Dict[str, Any] = field(default_factory=dict)
    # What the source looked at, and why it offered fewer. A source that filters (an answered
    # comment thread, a note a run already replied to) sets these; the engine copies them onto the
    # ``SourceReport``, so an empty channel can say whether it was empty or merely quiet.
    considered: int = 0
    explanation: str = ""
    # The record of what became of past items (``runner.feedback_ledger``), when the deployment
    # keeps one. A source consults it so that STATUS decides whether something is still owed,
    # rather than the circumstantial evidence it had to use before (a reply appearing under a
    # comment, a run finishing later the same day).
    ledger: Any = None
    # Reads this (card, source)'s stored snapshot for a key, or None. Set by the engine.
    snapshot_reader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
    # What the source wants stored for its next look, by key. Copied to the ``SourceReport`` and
    # only written when a run is actually handed the result (``ContextUpdates.mark_seen``).
    remembered: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def prior_snapshot(self, key: str) -> Optional[Dict[str, Any]]:
        """What this source stored under ``key`` the last time a run was handed its results."""
        if self.snapshot_reader is None:
            return None
        try:
            return self.snapshot_reader(key)
        except Exception as e:  # noqa: BLE001 -- a snapshot is an aid to explanation, never required
            log.info("context updates: could not read a snapshot (%s)", e)
            return None

    def remember(self, key: str, snapshot: Dict[str, Any]) -> None:
        """Ask for ``snapshot`` to be stored under ``key`` once this read has been delivered."""
        self.remembered[key] = snapshot

    def recorded(self, item_id: str) -> Any:
        """This item's ledger row, or None when nothing is tracked."""
        if self.ledger is None or not item_id:
            return None
        return self.ledger.get(self.card_id, self.spec.get("source") or "", item_id)

    def account(self, considered: int, explanation: str = "") -> None:
        """Say what this read looked at, and in one line why fewer items came back."""
        self.considered = int(considered)
        self.explanation = explanation

    def opt(self, key: str, default: Any = None) -> Any:
        """One option from this card's spec for this source."""
        return self.spec.get(key, default)


def _card_label(card: Dict[str, Any]) -> str:
    """A short human label for a card: its name or title, never its outcome/description.

    An outcome is a sentence about the future and a description is a paragraph; either one in a
    one-line manifest column crowds out the information the column exists for.
    """
    for key in ("name", "title", "quest_name", "label"):
        value = str((card or {}).get(key) or "").strip()
        if value:
            return value
    return ""
