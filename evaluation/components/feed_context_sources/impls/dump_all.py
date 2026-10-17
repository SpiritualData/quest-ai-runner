"""BASELINE: a channel with no rules. Everything the quest holds is offered on every look (every note whoever
wrote it, every goal update, every decision, however old or already seen) and every note is called an open
ask. What an update engine must beat: it is complete and it is noise, and it echoes the assistant's own notes
back as news. Zero model calls."""
from feed_context_sources.fake_quest import FakeQuest


def build(meter):  # noqa: ARG001
    def feed(kind, world):
        client = FakeQuest(world)
        ids, asks = [], []
        if kind in ("notes", "mixed"):
            for n in client.list_quest_notes("q"):
                ids.append(n["id"])
                asks.append(n["id"])
        if kind in ("goal_updates", "mixed"):
            for updates in client.list_quest_goal_updates("q").values():
                ids.extend(u["updateId"] for u in updates)
        if kind in ("decisions", "mixed"):
            ids.extend(d["decision_id"] for d in client.list_decisions_for_quest("q"))
        return {"offered": sorted(ids), "needs_response": sorted(asks)}
    return feed
