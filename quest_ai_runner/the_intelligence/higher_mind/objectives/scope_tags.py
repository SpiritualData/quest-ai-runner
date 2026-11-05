"""Quest's use of the hub's scope-tag fence: the kind of scope key a quest produces, and the note a quest-bound turn is told.

The fence itself (``as_tag_list``, ``scope_tags_allow``, ``scope_tags_from_keys``, ``union_scope_tags``) is generic and lives in
``hubofhubs.recall.scope_tags``; importers take those names from there. What knows Quest stays here: ``quest`` is the kind of
key this fence exists to isolate (``core.recent_context.quest_scope_key`` produces ``"quest:<id>"``), and the scope note names
quests.
"""
from __future__ import annotations

from typing import List, Sequence

from typing import Any, Mapping

from hubofhubs.api import ScopeTags, as_tag_list, scope_tags_allow, scope_tags_from_keys as hub_scope_tags_from_keys

QUEST_KINDS = ("quest",)

# QUEST SCOPE MODE (``CoreSettings.quest_scope_mode`` / QAR_QUEST_SCOPE_MODE). ``fence`` is today's behaviour: a quest tag is
# a hard scope tag, so an item learned on one quest is invisible to a turn on another. ``affinity`` maps the same quest tags
# onto the hub's SOFT affinity tags (``hubofhubs.recall.affinity_tags``): nothing is hidden by quest, items of the turn's own
# quest rank first, and a rendered item from another quest says where it came from. Items keep the quest tags they were
# stamped with in both modes, so switching mode never loses data.
QUEST_SCOPE_FENCE = "fence"
QUEST_SCOPE_AFFINITY = "affinity"

# Audience kinds still hard-fence in affinity mode (who may SEE an item). QAR stamps no team/org/user/person scope keys
# today, so this is empty and the fence is empty in affinity mode rather than invented.
AUDIENCE_KINDS: tuple = ()

# Ranking weights in affinity mode. PLACEHOLDERS: the private context-needs eval (cross_quest_recall against noise) tunes them.
AFFINITY_HOME_BOOST = 1.5
AFFINITY_AWAY_FACTOR = 0.6


def quest_scope_mode() -> str:
    """The quest scope mode in force: ``"fence"`` (default) or ``"affinity"``. Never raises."""
    try:
        from quest_ai_runner.the_body.configuration.runtime_settings import core_settings
        mode = str(core_settings().quest_scope_mode or "").strip().lower()
        return mode if mode in (QUEST_SCOPE_FENCE, QUEST_SCOPE_AFFINITY) else QUEST_SCOPE_FENCE
    except Exception:  # noqa: BLE001
        return QUEST_SCOPE_FENCE


def affinity_mode() -> bool:
    return quest_scope_mode() == QUEST_SCOPE_AFFINITY


def fence_tags(tags: ScopeTags) -> List[str]:
    """The tags that still HARD-fence: all of them in fence mode, only audience kinds in affinity mode. Never raises."""
    listed = as_tag_list(tags)
    if not affinity_mode():
        return listed
    return [t for t in listed if t.split(":", 1)[0] in AUDIENCE_KINDS and ":" in t]


def quest_fence_allow(item_tags: ScopeTags, turn_tags: ScopeTags) -> bool:
    """The hub's ``scope_tags_allow`` over only the tags that fence in the current mode. Identical to it in fence mode."""
    if not affinity_mode():
        return scope_tags_allow(item_tags, turn_tags)
    return scope_tags_allow(fence_tags(item_tags), fence_tags(turn_tags))


def quest_affinity_tags(tags: ScopeTags) -> List[str]:
    """The quest-kind tags among ``tags``: what an item or a turn is ABOUT in affinity mode. Never raises."""
    return hub_scope_tags_from_keys(tags, QUEST_KINDS)


def affinity_meta(meta: Mapping[str, Any]) -> dict:
    """``meta`` as the hub's arms should see it: in affinity mode the quest tags move from ``scope_tags`` (fence) to
    ``affinity_tags`` (rank). In fence mode ``meta`` is returned unchanged (same object)."""
    try:
        if not affinity_mode() or not meta:
            return meta  # type: ignore[return-value]
        tags = as_tag_list(meta.get("scope_tags"))
        out = dict(meta)
        out["affinity_tags"] = list(dict.fromkeys(as_tag_list(meta.get("affinity_tags")) + quest_affinity_tags(tags)))
        out["scope_tags"] = fence_tags(tags)
        return out
    except Exception:  # noqa: BLE001
        return meta  # type: ignore[return-value]


def card_affinity_factor(card: Mapping[str, Any], turn_tags: ScopeTags) -> float:
    """Ranking multiplier for ``card`` against the turn's quest tags: 1.0 in fence mode or with no tags on a side. Never raises."""
    try:
        if not affinity_mode():
            return 1.0
        from hubofhubs.api import affinity_factor
        return affinity_factor(quest_affinity_tags(card_scope_tags(card)), quest_affinity_tags(turn_tags),
                               home_boost=AFFINITY_HOME_BOOST, away_factor=AFFINITY_AWAY_FACTOR)
    except Exception:  # noqa: BLE001
        return 1.0


def other_quest_label(card: Mapping[str, Any], turn_tags: ScopeTags) -> str:
    """In affinity mode, the quest a card came from when it is not the turn's quest (its title when the card carries one,
    else the tag), else ``""``. Never raises."""
    try:
        if not affinity_mode():
            return ""
        item, turn = quest_affinity_tags(card_scope_tags(card)), quest_affinity_tags(turn_tags)
        if not item or not turn or not set(item).isdisjoint(turn):
            return ""
        return str(card.get("quest_title") or "").strip() or ", ".join(item)
    except Exception:  # noqa: BLE001
        return ""


def card_scope_tags(card: Mapping[str, Any]) -> List[str]:
    """The tags that fence ``card``: a card the backend derived from ONE quest (``managed_by`` is ``quest`` and it names a
    ``quest_id``) is fenced by that quest alone, whatever tags later turns unioned onto it; every other card by its own
    ``scope_tags``. Never raises."""
    try:
        if card.get("managed_by") == "quest" and card.get("quest_id"):
            return [f"quest:{card['quest_id']}"]
        return as_tag_list(card.get("scope_tags"))
    except Exception:  # noqa: BLE001
        return []


def card_visible_to_turn(card: Mapping[str, Any], turn_tags: ScopeTags) -> bool:
    """Whether ``card`` may reach a turn carrying ``turn_tags``. A card the backend derived from ONE quest (``managed_by`` is
    ``quest`` and it names a ``quest_id``) is about that quest alone, whatever tags later turns unioned onto it: it is
    visible only to a turn scoped to that quest, or to no quest. Every other card follows its own tags. Never raises."""
    try:
        return quest_fence_allow(card_scope_tags(card), turn_tags)
    except Exception:  # noqa: BLE001
        return True


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
    "offer to look there once they open that quest, without naming that quest or quoting anything from it yourself (its title is as private as its records). Things that belong to no quest (team agreements, general notes) stay usable.")


SCOPE_AFFINITY_NOTE = (
    "--- THIS CONVERSATION'S SCOPE (set by the system) ---\n"
    "This conversation is about {which}. Context lines marked \"from another quest\" were learned elsewhere: use one only "
    "when the question needs it, say where it came from, and prefer what this quest records when the two disagree.")


def scope_fence_block(quest_ids: ScopeTags) -> str:
    """The scope note for a turn bound to ``quest_ids``, or ``""`` when it is bound to none. Never raises."""
    try:
        ids = as_tag_list(quest_ids)
        if not ids:
            return ""
        one = len(ids) == 1
        if affinity_mode():
            return SCOPE_AFFINITY_NOTE.format(which=f"quest {ids[0]}" if one else "quests " + ", ".join(ids[:6]))
        return SCOPE_FENCE_NOTE.format(
            which=f"quest {ids[0]}" if one else "quests " + ", ".join(ids[:6]),
            those="that quest" if one else "those quests", the_id="the id" if one else "those ids")
    except Exception:  # noqa: BLE001
        return ""
