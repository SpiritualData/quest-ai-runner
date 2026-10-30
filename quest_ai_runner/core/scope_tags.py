"""Quest's use of the hub's scope-tag fence: the kind of scope key a quest produces, and the note a quest-bound turn is told.

The fence itself (``as_tag_list``, ``scope_tags_allow``, ``scope_tags_from_keys``, ``union_scope_tags``) is generic and lives in
``hubofhubs.recall.scope_tags``; importers take those names from there. What knows Quest stays here: ``quest`` is the kind of
key this fence exists to isolate (``core.recent_context.quest_scope_key`` produces ``"quest:<id>"``), and the scope note names
quests.
"""
from __future__ import annotations

from typing import List, Sequence

from hubofhubs.recall.scope_tags import ScopeTags, as_tag_list, scope_tags_from_keys as hub_scope_tags_from_keys

QUEST_KINDS = ("quest",)


def scope_tags_from_keys(scope_keys: ScopeTags, kinds: Sequence[str] = QUEST_KINDS) -> List[str]:
    """The keys among ``scope_keys`` whose kind is in ``kinds`` (default: ``quest``), in order, deduped: the tags a turn
    carries (``"quest:x"``). Never raises."""
    return hub_scope_tags_from_keys(scope_keys, kinds)


#: What a turn bound to one or more quests tells the model about its scope. The card fence above keeps another
#: quest's CARDS out of context; a read the model issues itself (a grep, a collection query, a hand-off) can still
#: return another quest's records, and a cheap model answers from whatever it holds. The ids come from the turn's
#: own quest scope (``meta["quest_id"]`` / ``meta["quest_ids"]``), set by the caller, never from words the model wrote.
SCOPE_FENCE_NOTE = (
    "--- THIS CONVERSATION'S SCOPE (set by the system) ---\n"
    "This conversation is about {which}. \"My budget\", \"my goals\", \"my notes\" and every other question about "
    "the person's own records mean the records of {those}. Records that belong to a different quest are private to "
    "it: never state, count or use them to answer, even when a read returned them (compare the quest id on a record "
    "with {the_id} above). When the answer exists only in another quest, say it is not recorded in this quest and "
    "offer to look there if they name it. Things that belong to no quest (team agreements, general notes) stay usable.")


def scope_fence_block(quest_ids: ScopeTags) -> str:
    """The scope note for a turn bound to ``quest_ids``, or ``""`` when it is bound to none. Never raises."""
    try:
        ids = as_tag_list(quest_ids)
        if not ids:
            return ""
        one = len(ids) == 1
        return SCOPE_FENCE_NOTE.format(
            which=f"quest {ids[0]}" if one else "quests " + ", ".join(ids[:6]),
            those="that quest" if one else "those quests", the_id="the id" if one else "those ids")
    except Exception:  # noqa: BLE001
        return ""
