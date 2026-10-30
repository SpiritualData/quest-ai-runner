"""scope_tags -- one generic cross-arm fence so a context item scoped to one tenant-like grouping
(e.g. a quest) never answers a turn scoped to a different one.

THE PROBLEM
-----------
A consumer that runs one assistant across many "quests" (or projects, or workspaces) needs a fact
learned while working on quest X to stay out of an answer given inside quest Y. Several retrieval
arms in this library persist and re-surface items across turns/conversations (the recent-context
store, the per-card vector store, the keyword card store); none of them previously had any notion of
"this item belongs to that quest" versus "this turn is asking about a different quest".

THE CONCEPT
-----------
An item (a card, a vector hit, a recent-context record) MAY carry ``scope_tags: ["<kind>:<id>",
...]``. A turn MAY carry the same vocabulary in its ``meta``/``context_meta`` under the
``"scope_tags"`` key. The vocabulary is whatever a consumer's scope keys already are (this repo's
own ``core.recent_context.quest_scope_key`` produces ``"quest:<id>"``, for example); this module does
not mint or parse the vocabulary, it only compares tag sets.

THE FENCE RULE
--------------
``scope_tags_allow(item_tags, turn_tags)`` is the single pure predicate every arm applies:

  * the item has NO tags -> visible everywhere (untagged is legacy data / general knowledge, never
    hidden by this fence);
  * the turn has NO tags -> the caller isn't scoping this turn to anything, so nothing is hidden;
  * otherwise -> visible only when the two tag sets intersect.

This is deliberately a POST-filter, not a payload-level exact-match query: a vector store's own
``scope`` filter (a tenant/user id) is a hard, indexed, single-value partition, while scope_tags is a
many-valued, optional, itemwise membership check applied to whatever the store already returned.

Never raises: any malformed input degrades to the safest reading for that side (an unparseable item
tag list is empty -- so item defaults to being visible -- and would raise nothing since inputs are
just normalized).
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Union

ScopeTags = Optional[Union[str, Sequence[str]]]


def as_tag_list(tags: ScopeTags) -> List[str]:
    """Normalize ``tags`` (a bare string, a list/tuple, or None/falsy) into a deduped list of
    non-empty strings. Never raises."""
    try:
        if not tags:
            return []
        if isinstance(tags, str):
            return [tags] if tags else []
        return list(dict.fromkeys(str(t) for t in tags if t))
    except Exception:  # noqa: BLE001
        return []


def scope_tags_allow(item_tags: ScopeTags, turn_tags: ScopeTags) -> bool:
    """True when ``item_tags`` should be visible to a turn carrying ``turn_tags``.

    True when the item has no tags, or the turn has no tags, or the two tag sets intersect. Never
    raises."""
    try:
        item_list = as_tag_list(item_tags)
        if not item_list:
            return True
        turn_list = as_tag_list(turn_tags)
        if not turn_list:
            return True
        return not set(item_list).isdisjoint(turn_list)
    except Exception:  # noqa: BLE001
        return True


def scope_tags_from_keys(scope_keys: ScopeTags, kinds: Sequence[str] = ("quest",)) -> List[str]:
    """The keys among ``scope_keys`` whose kind (the text before the first ``":"``) is in
    ``kinds``, in order, deduped. A bare key with no ``":"`` (e.g. ``"global"``, or a bare
    conversation id passed directly) has no kind and never becomes a tag. Default ``kinds``
    is ``("quest",)``, the kind this fence exists to isolate.

    This is the single, shared way every scope-key-consuming module derives its ``scope_tags``
    (the quest-kind keys "in scope this turn") from a list of scope keys such as ``["conv:a",
    "quest:x", "global"]`` -> ``["quest:x"]`` -- centralizing what ``core.recent_context`` and
    ``core.anticipation`` each used to compute with their own inline comprehension. Never raises.
    """
    out: List[str] = []
    try:
        kind_set = set(kinds or ())
        for key in as_tag_list(scope_keys):
            if ":" not in key:
                continue
            kind = key.split(":", 1)[0]
            if kind in kind_set and key not in out:
                out.append(key)
    except Exception:  # noqa: BLE001
        return out
    return out


def union_scope_tags(*tag_sources: ScopeTags) -> List[str]:
    """Union several scope-tag sources into one deduped list, preserving first-seen order. Any
    falsy/malformed source contributes nothing. Never raises."""
    out: List[str] = []
    try:
        for source in tag_sources:
            for tag in as_tag_list(source):
                if tag not in out:
                    out.append(tag)
    except Exception:  # noqa: BLE001
        return out
    return out


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
