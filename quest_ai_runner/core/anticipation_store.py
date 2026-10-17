"""Anticipation engine, persistence: rebuilding stored ``Pattern`` / ``Prediction`` records from
dicts and ``FilePredictionStore``, the runner lane's atomic one-JSON-file-per-scope store plus the
outcome log. Best-effort and never raises.

``core.anticipation`` re-exports every name here; the learning functions are in
``anticipation_learner`` and the per-turn ``Anticipator`` is in ``anticipation_engine``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .anticipation_learner import (
    CONF_FLOOR,
    Pattern,
    Prediction,
)
from .file_modes import match_umask

log = logging.getLogger("quest-ai-runner.anticipation")


def pattern_from_dict(d: Dict[str, Any]) -> Optional[Pattern]:
    """Rebuild a ``Pattern`` from a stored dict, tolerating missing fields. None when unusable.

    Public: any consumer with its own storage (e.g. the Mongo-backed twin in quest-backend)
    rebuilds stored records through this, rather than porting the field mapping."""
    try:
        if not isinstance(d, dict) or not d.get("pattern_id"):
            return None
        return Pattern(
            pattern_id=str(d["pattern_id"]),
            scope=str(d.get("scope", "")),
            hour_bucket=int(d.get("hour_bucket", 0)),
            dow=int(d.get("dow", 0)),
            is_weekend=bool(d.get("is_weekend", False)),
            canonical_text=str(d.get("canonical_text", "")),
            keywords=[str(x) for x in (d.get("keywords") or [])],
            weight=float(d.get("weight", CONF_FLOOR)),
            hits=int(d.get("hits", 0)),
            misses=int(d.get("misses", 0)),
            last_seen_ts=float(d.get("last_seen_ts", 0.0)),
            created_ts=float(d.get("created_ts", 0.0)),
            display_text=str(d.get("display_text") or ""),
        )
    except Exception:  # noqa: BLE001
        return None


def prediction_from_dict(d: Dict[str, Any]) -> Optional[Prediction]:
    """Rebuild a ``Prediction`` from a stored dict, tolerating missing fields. None when unusable.

    Public, for the same reason as ``pattern_from_dict`` above."""
    try:
        if not isinstance(d, dict) or not d.get("prediction_id"):
            return None
        return Prediction(
            prediction_id=str(d["prediction_id"]),
            text=str(d.get("text", "")),
            confidence=float(d.get("confidence", 0.0)),
            source=str(d.get("source", "pattern")),
            created_ts=float(d.get("created_ts", 0.0)),
            expires_ts=float(d.get("expires_ts", 0.0)),
            context_card_ids=[str(x) for x in (d.get("context_card_ids") or [])],
            draft_answer=d.get("draft_answer"),
            display_text=str(d.get("display_text") or ""),
            scope_tags=[str(x) for x in (d.get("scope_tags") or [])],
        )
    except Exception:  # noqa: BLE001
        return None


# Deprecated underscore aliases, kept for ONE release so an in-flight consumer pinned to an older
# version keeps importing. Remove after the next release; use the public names above.
_pattern_from_dict = pattern_from_dict
_prediction_from_dict = prediction_from_dict


class FilePredictionStore:
    """The runner lane's persistence for the anticipation engine: one JSON file per SCOPE KEY
    under ``<root_dir>/predictions/<sha1(key)[:16]>.json`` (the same layout convention as
    ``core.recent_context.FileRecentContextStore``), written atomically (tempfile +
    ``os.replace``, the ``adapters.card_repository`` convention).

    Each scope file holds ``{"patterns": [...], "predictions": [...]}``; each stored prediction
    record carries an optional ``"context_view"`` sidecar (the precomputed bundle text, kept OUT
    of the shared ``Prediction`` dataclass on purpose: it is a runner-lane persistence detail).
    Outcome records append to ``<root_dir>/predictions/prediction_log.jsonl`` (one JSON object
    per line) so accuracy (hit rate, mean score) is measurable offline. Every method is
    best-effort and never raises.
    """

    def __init__(self, root_dir: str = ".quest-context"):
        self._dir = Path(root_dir) / "predictions"

    @property
    def log_path(self) -> Path:
        """Where outcome records append (JSONL, one object per line)."""
        return self._dir / "prediction_log.jsonl"

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{digest}.json"

    def _load_state(self, key: str) -> Dict[str, Any]:
        path = self._path(key)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, key: str, state: Dict[str, Any]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._dir), prefix=".tmp_", suffix=".json")
            match_umask(tmp_fd)  # mkstemp makes 0600; another account sharing the corpus must read it
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp_path, str(self._path(key)))
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            log.debug("FilePredictionStore write failed for key %r", key, exc_info=True)

    # --- patterns -------------------------------------------------------------------------------

    def load_patterns(self, scope_key: str) -> List[Pattern]:
        """The stored patterns for ``scope_key``. [] on any failure."""
        try:
            raw = self._load_state(scope_key).get("patterns") or []
            return [p for p in (pattern_from_dict(d) for d in raw) if p is not None]
        except Exception:  # noqa: BLE001
            return []

    def save_patterns(self, scope_key: str, patterns: List[Pattern]) -> None:
        """Persist ``patterns`` for ``scope_key``, preserving the scope's live predictions."""
        try:
            state = self._load_state(scope_key)
            state["patterns"] = [asdict(p) for p in (patterns or [])]
            self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("save_patterns failed for key %r", scope_key, exc_info=True)

    # --- live predictions -----------------------------------------------------------------------

    def load_predictions(self, scope_key: str) -> List[Prediction]:
        """The LIVE (unexpired) predictions for ``scope_key``. Expired ones are silently
        dropped on load. [] on any failure."""
        try:
            now_ts = time.time()
            raw = self._load_state(scope_key).get("predictions") or []
            out: List[Prediction] = []
            for d in raw:
                p = prediction_from_dict(d)
                if p is not None and p.expires_ts > now_ts:
                    out.append(p)
            return out
        except Exception:  # noqa: BLE001
            return []

    def save_predictions(
        self, scope_key: str, predictions: List[Prediction],
        views: Optional[Dict[str, str]] = None,
    ) -> None:
        """Persist the scope's live predictions (REPLACING the previous set), preserving its
        patterns. ``views`` maps prediction_id to its precomputed context_view text (the bundle),
        stored as a sidecar field on each prediction record."""
        try:
            state = self._load_state(scope_key)
            records: List[Dict[str, Any]] = []
            for p in predictions or []:
                rec = asdict(p)
                view = (views or {}).get(p.prediction_id, "")
                if view:
                    rec["context_view"] = view
                records.append(rec)
            state["predictions"] = records
            self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("save_predictions failed for key %r", scope_key, exc_info=True)

    def load_view(self, scope_key: str, prediction_id: str) -> str:
        """The precomputed context_view stored for ``prediction_id`` under ``scope_key``
        ("" when none was precomputed or on any failure)."""
        try:
            for d in self._load_state(scope_key).get("predictions") or []:
                if isinstance(d, dict) and d.get("prediction_id") == prediction_id:
                    return str(d.get("context_view") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def clear_predictions(self, scope_key: str) -> None:
        """Drop the scope's live predictions (they were judged this turn), keeping patterns."""
        try:
            state = self._load_state(scope_key)
            if state.get("predictions"):
                state["predictions"] = []
                self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("clear_predictions failed for key %r", scope_key, exc_info=True)

    # --- outcome log ------------------------------------------------------------------------------

    def append_log(self, record: Dict[str, Any]) -> None:
        """Append one outcome record to ``prediction_log.jsonl`` (best-effort append-only)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.debug("append_log failed", exc_info=True)
