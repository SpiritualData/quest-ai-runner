"""Which person, goal or post a phrase refers to, settled by the hub's Resolve a Reference, with an abstain when it is none of them.

Role: the Quest side of the one solution that ties a phrase to a candidate. It shapes the quest's people (members) and goals as candidates, asks ``resolve-a-reference`` through the
front door, and returns the answer in the form each caller already used: a user id or the reason there is none, a goal or nothing. A person named by id, email, name or the words of a
name is found only when exactly one member fits ("Ann" with two Anns is not Ann Lee); a title the quest already holds is its goal only when the whole title is the same; a post is
paired with a goal only when the model, shown both, says it is that very post. An abstain is never turned into the closest candidate: the caller says it did not find the thing, or
creates, or asks. This module never decides whether a goal may be changed: that stays with ``goal_links.is_owned`` (a title ending ``next step: week of YYYY-MM-DD`` or a description
that begins ``Kept by Quest AI:``), which the callers apply after a match, so a match is never a licence to write.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("quest-ai-runner.references")
SOLUTION = "resolve-a-reference"
EXACT = "exact_match"
JUDGED = "model_judged"


def member_candidates(members: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The quest's members as candidates: the user id, the name, and the email (whole, and its part before the @) as aliases. A member with no user id cannot be an owner and is left out."""
    out = []
    for m in members:
        if not m.get("user_id"):
            continue
        email = str(m.get("email") or "").strip()
        out.append({"id": str(m["user_id"]), "name": str(m.get("name") or ""), "aliases": [a for a in (email, email.split("@")[0] if "@" in email else "") if a]})
    return out


def goal_candidates(goals: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Goals as candidates, whichever field names the listing used (title or name, text or description)."""
    return [{"id": str(g.get("id")), "name": str(g.get("title") if g.get("title") is not None else g.get("name") or ""),
             "text": str(g.get("text") if g.get("text") is not None else g.get("description") or "")} for g in goals if g.get("id")]


def resolve(phrase: str, candidates: Sequence[Mapping[str, Any]], *, ask: Optional[Callable[[str], str]] = None, whole: bool = False) -> Dict[str, Any]:
    """The hub's answer (``outcome`` match or abstain, ``id``, ``reason``). With ``ask`` the model-judged arm settles it with that one call; without, the exact arm does, whole titles only
    when ``whole``. A model answer that cannot be used is an abstain, never a guess."""
    import quest_ai_runner
    arm = JUDGED if ask is not None else EXACT
    given: Dict[str, Any] = {"phrase": phrase, "candidates": list(candidates)}
    if ask is not None:
        given["ask"] = ask
    if whole:
        given["context"] = {"whole": True}
    try:
        return dict(quest_ai_runner.run(SOLUTION, given, arms={SOLUTION: arm}))
    except Exception as error:      # noqa: BLE001  the model's answer was not a candidate offered, or the call failed: nothing is matched
        log.warning("reference not settled by %s: %s: %s", arm, type(error).__name__, str(error)[:200])
        return {"outcome": "abstain", "id": "", "reason": "the answer could not be used"}


def member_reference(who: Any, members: Sequence[Mapping[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """``(user_id, None)`` for the one member ``who`` names (by id, email, name or the words of a name), else ``(None, why)`` naming the people it could have been."""
    if not str(who or "").strip():
        return None, "a human step needs an assignee"
    answer = resolve(str(who), member_candidates(members))
    if answer["outcome"] == "match":
        return answer["id"], None
    roster = ", ".join(str(m.get("name") or m["user_id"]) for m in members if m.get("user_id")) or "nobody"
    return None, f"{who!r} does not name exactly one person on this quest. Members: {roster}."


def goal_titled(title: str, goals: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The one goal whose whole title is ``title`` (case and punctuation aside), else None."""
    answer = resolve(title, goal_candidates(goals), whole=True)
    return next((g for g in goals if str(g.get("id")) == answer["id"]), None) if answer["outcome"] == "match" else None


def goal_offered(goal_id: Any, offered: Sequence[Mapping[str, Any]]) -> str:
    """``goal_id`` when it is the id of one of the goals ``offered`` to a model, else "": an id that was not offered is never adopted."""
    answer = resolve(str(goal_id or ""), goal_candidates(offered), whole=True)
    return answer["id"] if answer["outcome"] == "match" else ""


def post_goal(text: str, candidates: Sequence[Mapping[str, Any]], ask: Callable[[str], str]) -> Optional[Mapping[str, Any]]:
    """The goal that is the very same post as ``text`` (a version of a piece of content), judged by one model call; None when none is, or the answer cannot be used."""
    if not candidates:
        return None
    answer = resolve(text, goal_candidates(candidates), ask=ask)
    return next((g for g in candidates if str(g.get("id")) == answer["id"]), None) if answer["outcome"] == "match" else None
