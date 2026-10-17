"""Runs a cache_cards scenario on the live ``QuestApiCardRepository`` with the cache an impl supplies."""
from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
from tests.quest_card_backend_fake import FakeCardBackend


def run_scenario(args, make_cache):
    backend = FakeCardBackend()
    cache = make_cache()
    repo = QuestApiCardRepository(base_url=backend.url, api_key="k", cache=cache, timeout=2.0)
    cards, write_ok, up = [], None, True
    try:
        for step in args["steps"]:
            op = step[0]
            if op == "put":
                backend.put(step[1], {"name": step[1], "summary": "s"})
            elif op == "load":
                cards = sorted(repo.load_all())
            elif op == "write":
                write_ok = repo.write(step[1], {"name": step[1], "summary": "s"})
            elif op == "down":
                backend.close()
                up = False
            elif op == "new_repo":
                repo = QuestApiCardRepository(base_url=backend.url, api_key="k", cache=cache, timeout=2.0)
            else:
                raise ValueError(op)
        stored = cache.load_listing()
        health = repo.health()
        return {"cards": cards, "write_ok": write_ok, "cache_stored": len(stored[0]) if stored else 0,
                "cache_pending": len(cache.pending_writes()), "loud": bool(health["degraded"] and health["error_count"] > 0)}
    finally:
        if up:
            backend.close()
