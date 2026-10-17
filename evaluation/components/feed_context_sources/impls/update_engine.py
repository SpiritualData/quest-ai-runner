"""CURRENT: the live ``UpdateEngine`` with the real quest channels (``QuestNotesSource``, ``GoalUpdatesSource``,
``DecisionsSource``) reading a fake Quest client. A case's ``kind`` selects the channels watched; the engine's
own bounds (first-look window, open-ask age and count) apply. Zero model calls."""
from datetime import datetime

from feed_context_sources.fake_quest import FakeQuest
from quest_ai_runner.runner.context_updates import UpdateEngine
from quest_ai_runner.runner.context_sources_quest import DecisionsSource, GoalUpdatesSource, QuestNotesSource

CHANNELS = {"notes": ("quest_notes",), "goal_updates": ("goal_updates",), "decisions": ("decisions",),
            "mixed": ("quest_notes", "goal_updates", "decisions")}


def build(meter):  # noqa: ARG001
    def feed(kind, world):
        now = datetime.fromisoformat(world["now"])
        engine = UpdateEngine(FakeQuest(world), sources=[QuestNotesSource(), GoalUpdatesSource(), DecisionsSource()],
                              always=CHANNELS[kind], now_fn=lambda: now)
        since = datetime.fromisoformat(world["since"]) if world.get("since") else None
        bundle = engine.collect({"quest_id": "quest_a"}, card_id="quest_a", since=since)
        return {"offered": sorted(u.item_id for u in bundle.updates),
                "needs_response": sorted(u.item_id for u in bundle.updates if u.needs_response)}
    return feed
