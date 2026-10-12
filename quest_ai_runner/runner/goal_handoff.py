"""Goal assignment and AI handoff for autopilot.

Two jobs, both judged by one cheap model call (``judge``: prompt text -> raw text) and both
best-effort (a failure never fails a pass):

* ``choose_assignee`` -- when autopilot creates a goal, pick which HUMAN member of the quest it
  belongs to, and whether an AI can do the work itself.
* ``run_goal_handoff`` -- for goals already assigned to a human whose AI rep is on the team, judge
  whether the rep should handle it and ask the backend to hand it over.

The backend applies the assignee's own preference (auto / ask / never), so nothing here reads it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.goal_handoff")

MAX_HANDOFF_JUDGMENTS_PER_PASS = 5

ASSIGNEE_PROMPT = """You are deciding who a newly created goal belongs to on a shared quest.

Quest outcome: {outcome}
Where the quest stands now: {current_state}
The owner's preferences: {preferences}
Goal: {title}
Details: {description}

Human members of the quest, with their role on the team (choose only from these ids):
{members}

Rules:
- Assign to the person who should actually do or decide the goal, judging by their role and what
  they do. Work that needs one specific person's identity, relationships, presence, taste, money
  or sign-off goes to that person; do not default every goal to the owner.
- If an AI can do the work itself (research, drafting, audits, documentation, comparisons, for
  example "Identify and document the top 3 'False Balance' misapplications on Wikipedia pages"),
  set ai_can_do to true and still name the human whose AI rep should do it; it must not sit as a
  plain task on a person's list.
- If it is truly unknown who it belongs to, use null. On a quest with two or more people that
  leaves the goal with no owner: it is hidden from every member and the quest owner is asked to
  pick someone, so use null only when you cannot reasonably choose.
- Never use an em dash in the reason.

Answer with one JSON object only: {{"assignee": "<user id or null>", "ai_can_do": true|false, "reason": "<short>"}}"""

DOABILITY_PROMPT = """A goal is assigned to a person. Their AI representative may take it over, but only if the
AI can finish it completely on its own. The person has not been asked, so a wrong yes costs them a
goal they must then take back.

Quest outcome: {outcome}
Where the quest stands now: {current_state}
The owner's preferences: {preferences}
Goal: {title}
Details: {description}

Judge sufficiency, not resemblance. Ask: with only text, the files and data it can read, and the
ability to write documents, tables and code, could an AI produce everything this goal needs so that
the person only has to review it? Work of that kind: drafting a document or message, reading figures
or a report and summarizing them, researching and comparing options, auditing a page, writing test
files or code. Answer true only when the whole goal is covered.

Answer false when any necessary part needs the person's body or presence, a call or meeting, a
judgment call or taste decision that is theirs, spending or receiving money, their identity or
sign-off, publishing in their name, a real world action, or information the AI has no way to get.
Also answer false when the goal is too vague to tell what finished looks like. If it is only partly
doable, answer false.

Answer with one JSON object only: {{"ai_can_do": true|false, "reason": "<one short sentence, no em dashes>"}}"""


def make_judge(provider: Any, tier: str = "fast") -> Optional[Callable[[str], str]]:
    """Wrap a model provider as a ``prompt -> text`` judge, or None when there is no provider."""
    if provider is None:
        return None
    from .personas import _judge_model

    def judge(prompt: str) -> str:
        return provider.answer([{"role": "user", "content": prompt}],
                               model=_judge_model(provider, tier)) or ""
    return judge


def _parse(raw: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


AI_MEMBER_ROLES = ("ai_service",)


def is_human_member(m: Any) -> bool:
    """A member entry with an id that is a person: AI representatives are never assignees."""
    if not isinstance(m, dict) or not m.get("user_id"):
        return False
    return str(m.get("role") or "").strip().lower() not in AI_MEMBER_ROLES and not m.get("is_ai")


def human_members(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in members or [] if is_human_member(m)]


def human_member_ids(members: List[Dict[str, Any]]) -> List[str]:
    return [str(m.get("user_id")) for m in human_members(members)]


def preferences_text(value: Any) -> str:
    """A quest's preferences as prompt text: a string stays as it is, a dict or list is dumped as
    JSON (str() of a dict is Python repr, which reads badly in a prompt)."""
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=1, default=str)
    except (TypeError, ValueError):
        return str(value)


ROLE_LABELS = {"owner": "quest owner", "admin": "team admin", "member": "team member",
               "shared": "shared with the quest, not on the team"}


def member_line(m: Dict[str, Any]) -> str:
    """One roster line: id, name, and the person's role, so the judge can tell a lead from a
    contributor."""
    role = ROLE_LABELS.get(str(m.get("role") or ("owner" if m.get("is_owner") else "")), "")
    label = m.get("name") or m.get("email") or ""
    return f"- {m.get('user_id')}: {label}" + (f" ({role})" if role else "")


def default_owner_id(members: List[Dict[str, Any]]) -> Optional[str]:
    """The human the goal falls back to when nothing better can be chosen: the quest owner, else
    the first human member."""
    humans = human_members(members)
    for m in humans:
        if m.get("is_owner"):
            return str(m.get("user_id"))
    return str(humans[0].get("user_id")) if humans else None


def choose_assignee(judge: Optional[Callable[[str], str]], members: List[Dict[str, Any]], *,
                    outcome: str, title: str, description: str,
                    current_state: str = "", preferences: str = "") -> Dict[str, Any]:
    """Return ``{"assignee": id|None, "ai_can_do": bool}``. Never raises.

    On a quest with 2+ people an unassigned goal has no owner: it is hidden from every member and
    the quest owner is asked to pick someone. So a goal must not end up there only because the
    judge was missing, failed, answered nothing usable or named someone who is not a member: in
    each of those cases the goal goes to the quest owner (``default_owner_id``), who can hand it
    on. Only a judge that answers an explicit null leaves it without an owner, which is the
    judge's own "I cannot tell" and is exactly what the owner ask is for.
    """
    ids = human_member_ids(members)
    if not ids:
        return {"assignee": None, "ai_can_do": False}
    fallback = {"assignee": ids[0] if len(ids) == 1 else default_owner_id(members), "ai_can_do": False}
    if judge is None:
        return fallback
    try:
        listing = "\n".join(member_line(m) for m in human_members(members))
        verdict = _parse(judge(ASSIGNEE_PROMPT.format(
            outcome=outcome or "(none)", current_state=current_state or "(none)",
            preferences=preferences or "(none)", title=title,
            description=description or "(none)", members=listing)))
    except Exception:  # noqa: BLE001 -- a judge failure must not fail the pass
        log.info("goal_handoff: assignee judge failed", exc_info=True)
        return fallback
    if not verdict:
        return fallback
    chosen = verdict.get("assignee")
    chosen = str(chosen) if chosen not in (None, "", "null") else None
    if chosen is not None and chosen not in ids:
        return fallback
    return {"assignee": chosen, "ai_can_do": bool(verdict.get("ai_can_do")) and chosen is not None}


def rep_for_user(reps: List[Dict[str, Any]], user_id: str) -> Optional[str]:
    """The rep id representing ``user_id`` (default rep preferred), or None."""
    mine = [r for r in reps or [] if str(r.get("owner_user_id") or "") == str(user_id)]
    if not mine:
        return None
    mine.sort(key=lambda r: not r.get("is_default"))
    return str(mine[0].get("rep_id") or "") or None


def run_goal_handoff(client: Any, judge: Optional[Callable[[str], str]], goals_payload: Dict[str, Any],
                     *, team_id: Optional[str], outcome: str = "", current_state: str = "",
                     preferences: str = "", limit: int = MAX_HANDOFF_JUDGMENTS_PER_PASS) -> List[Dict[str, Any]]:
    """Judge undecided human-assigned goals and ask the backend to hand AI-doable ones to the rep.

    Returns one ``{goal_id, handling}`` per call made. Stamping "me" records the decision so the
    goal is not judged again next pass. Never raises.
    """
    done: List[Dict[str, Any]] = []
    if judge is None or not callable(getattr(client, "set_goal_ai_handling", None)):
        return done
    try:
        candidates = []
        for group in (goals_payload or {}).get("period_groups") or []:
            for g in group.get("goals") or []:
                if (g.get("completed") or g.get("abandoned") or not g.get("assigned_to_user_id")
                        or g.get("ai_handling") or g.get("ai_handling_decided_at")):
                    continue
                candidates.append(g)
        if not candidates:
            return done
        reps = client.list_team_reps(team_id=team_id) or []
        for g in candidates:
            if len(done) >= limit:
                break
            rep_id = rep_for_user(reps, g["assigned_to_user_id"])
            if not rep_id:
                continue
            try:
                verdict = _parse(judge(DOABILITY_PROMPT.format(
                    outcome=outcome or "(none)", current_state=current_state or "(none)",
                    preferences=preferences or "(none)", title=g.get("name") or "",
                    description=g.get("description") or "(none)")))
                if not verdict:
                    continue
                handling = "rep" if verdict.get("ai_can_do") is True else "me"
                client.set_goal_ai_handling(g["id"], handling, rep_id=rep_id, decided_by="rep")
                done.append({"goal_id": g["id"], "handling": handling})
            except Exception:  # noqa: BLE001 -- one goal's failure must not stop the rest
                log.info("goal_handoff: goal %s not handled", g.get("id"), exc_info=True)
    except Exception:  # noqa: BLE001
        log.info("goal_handoff: pass failed", exc_info=True)
    return done
