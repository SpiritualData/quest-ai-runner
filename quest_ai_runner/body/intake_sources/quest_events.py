"""Quest's events as an Intake Sources adapter, so the hub's Proactivity can count them.

A quest's asks ledger (``QuestClient.list_asks``) holds what people asked of the AI. Each ask row is an event at the time it was made (``occurred_at``),
which is what Proactivity compares with a quest's last pass when the quest is reactive. An ask with no readable time is the oldest event there is: it
counts for a quest that has never passed and never for one that has, so a malformed row cannot decide a gate on its own.
"""
from typing import Any, Dict, List, Mapping, Sequence

from esoi import implementation

SOURCE = "quest-asks"
OLDEST = "1970-01-01T00:00:00Z"


@implementation("intake-sources", "quest_events")
class QuestEvents:
    """The asks of one quest, each as an event Proactivity can compare with the quest's last pass."""
    REQUIRES = ()     # standard library plus the hub

    def __init__(self, asks: Sequence[Mapping[str, Any]] = ()) -> None:
        self.asks = list(asks)

    def sources(self) -> Sequence[Mapping[str, Any]]:
        return [{"kind": SOURCE, "description": "what people asked of the AI on a quest"}]

    def pull(self, source: str = "", limit: int = 10) -> Sequence[Mapping[str, Any]]:
        if source and source != SOURCE:
            return []
        events: List[Dict[str, Any]] = [{"text": str(ask.get("text") or ""), "person": str(ask.get("author") or ""), "source": SOURCE,
                                         "at": ask.get("occurred_at") or OLDEST} for ask in self.asks]
        return events[:limit]


ACTIVITY_SOURCE = "quest-activity"


@implementation("intake-sources", "quest_activity")
class QuestActivity:
    """What happened on a quest (a goal done, a habit logged, a note added, a finding decided, an experiment changed), each as an event Proactivity can compare with
    the quest's last look. The host reads its own records and passes them in as ``{"at": ISO time, "kind": ...}``; a record with no readable time is left out, so a
    malformed row never makes a quest due."""
    REQUIRES = ()     # standard library plus the hub

    def __init__(self, records: Sequence[Mapping[str, Any]] = ()) -> None:
        self.records = list(records)

    def sources(self) -> Sequence[Mapping[str, Any]]:
        return [{"kind": ACTIVITY_SOURCE, "description": "what happened on a quest since its last look"}]

    def pull(self, source: str = "", limit: int = 10) -> Sequence[Mapping[str, Any]]:
        if source and source != ACTIVITY_SOURCE:
            return []
        events: List[Dict[str, Any]] = [{"text": str(r.get("kind") or ""), "person": "", "source": ACTIVITY_SOURCE, "at": r["at"]} for r in self.records if r.get("at")]
        return events[:limit]
