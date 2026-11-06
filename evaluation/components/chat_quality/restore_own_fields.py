"""DEV ONLY: put the eval world's OWN quests' editable fields back to their seeded values.

An API key is always the AI to quest-backend's field-write gate (``ai_field_writes``), so a REST
PATCH from this harness's key is parked as an approvable proposal and never applied. That left a
field the AI edited during a case (for example ``current_state``) stuck at the case's value and
aborted every later reset with "quest ... NOT restored". The seeded values are the harness's own
fixtures, so this restores them in-process as the human actor, the same pattern and safety checks
as ``approve_own_asks.py`` (cwd = a quest-backend checkout, its interpreter, development backend
with a local Mongo) and only for quests whose acceptance criteria carry the eval tag.

The running server keeps a quest cache this process cannot clear; the caller (``world.py``) busts it
by appending and deleting a scratch note through the API (the atomic note write invalidates it).

Usage: restore_own_fields.py <json file: {quest_id: {field: value}}>
Prints one JSON line per field: {quest_id, field, ok, reason?}.
"""
import asyncio
import json
import sys

from approve_own_asks import TAG, check_dev_backend, refuse


async def restore(plan):
    from app.core.main_loop import set_main_event_loop
    from app.business.quests.quest_manager import QuestManager
    quest_manager = QuestManager()
    set_main_event_loop(asyncio.get_running_loop())
    for quest_id, fields in plan.items():
        quest = await quest_manager.get_quest(quest_id)
        criteria = str(getattr(quest, "acceptance_criteria", "") or "") if quest else ""
        if quest is None or not (TAG in criteria or TAG in str(fields.get("acceptance_criteria") or "")):
            print(json.dumps({"quest_id": quest_id, "ok": False,
                              "reason": "not found or not an eval-tagged quest"}))
            continue
        for field, value in fields.items():
            try:
                await quest_manager.update_quest_field(
                    quest_id=quest_id, field_name=field, value=value, user_id=None)
                print(json.dumps({"quest_id": quest_id, "field": field, "ok": True}))
            except Exception as e:  # noqa: BLE001 -- reported per field, the caller verifies
                print(json.dumps({"quest_id": quest_id, "field": field, "ok": False,
                                  "reason": f"{type(e).__name__}: {e}"[:200]}))


def main():
    if len(sys.argv) != 2:
        refuse("usage: restore_own_fields.py <plan.json>")
    check_dev_backend()
    with open(sys.argv[1]) as f:
        plan = json.load(f)
    asyncio.run(restore(plan))


if __name__ == "__main__":
    main()
