"""The no-model floor of Intent Evolution: what worked for this person before, else a small experiment, else restraint.

It reads only the quest it is handed (``intents``, ``metrics``, ``experiments``, ``history``) and calls no model, so every smarter arm has a measured
floor to beat on the same Quest cases. In order:

1. no intents, or an experiment still running: ``none`` (nothing to move, or the person is already finding out);
2. a past history entry that worked (``worked`` true, with ``text``): that option again, with the metric it was checked by;
3. otherwise an ``experiment`` on the first intent, checked by the quest's first metric (empty when the quest has none, so the experiment names the gap).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from esoi import implementation

RUNNING_STATUSES = ("running", "active", "open", "started")


def metric_name(metric: Any) -> str:
    return str(metric.get("name") or metric.get("title") or "") if isinstance(metric, Mapping) else str(metric or "")


def experiment_is_running(experiment: Any) -> bool:
    return isinstance(experiment, Mapping) and str(experiment.get("status", "")).lower() in RUNNING_STATUSES


def worked_before(history: Sequence[Any]) -> Mapping[str, Any]:
    """The most recent history entry that worked and says what it was, or an empty mapping."""
    for entry in reversed(list(history)):
        if isinstance(entry, Mapping) and entry.get("worked") and entry.get("text"):
            return entry
    return {}


@implementation("intent-evolution", "history_floor")
class HistoryFloor:
    """Repeats what worked, else proposes one small experiment, else holds back; asks no model."""
    REQUIRES = ()

    def suggest(self, quest: Mapping[str, Any]) -> Mapping[str, Any]:
        intents = [str(i) for i in quest.get("intents") or [] if str(i).strip()]
        if not intents or any(experiment_is_running(e) for e in quest.get("experiments") or []):
            return {"kind": "none", "text": "", "metric": ""}
        metrics = [metric_name(m) for m in quest.get("metrics") or []]
        metric = next((m for m in metrics if m), "")
        earlier = worked_before(quest.get("history") or [])
        if earlier:
            return {"kind": "option", "text": str(earlier["text"]), "metric": str(earlier.get("metric") or metric)}
        return {"kind": "experiment", "text": f"Try one small step toward \"{intents[0]}\" this week and note what changed.", "metric": metric}
