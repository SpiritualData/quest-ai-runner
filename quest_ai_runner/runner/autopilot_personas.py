"""Which persona does autopilot work: roster, on-duty and instruction lookups.

Split out of ``autopilot.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from datetime import datetime

from .autopilot_defaults import (
    BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS,
    BUNDLED_DEFAULT_QUEST_INSTRUCTIONS,
    DEFAULT_ASSISTANT_REP_ID,
    MAX_INSTRUCTIONS_CHARS,
    log,
)
from .autopilot_periods import _truthy, _weekday_abbrev


class _HeldPersona:
    """The type of :data:`PERSONA_HELD`. A singleton, so ``is`` is the whole test."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- a debugging aid, never user-facing
        return "PERSONA_HELD"


# Returned by the persona resolvers instead of a rep id when today's answer to "who works this" is
# NOBODY: the work belongs to a character the person rostered for other days.
#
# It is a third answer on purpose, and the reason is a real incident. A quest had Bailey rostered
# Mon-Fri and Batman rostered Sat. On a Saturday, Bailey produced work and emailed the owner,
# because a work item carrying her ``assignee_rep_id`` outranked her own roster entry -- her days
# were advisory. They are authoritative now, and this constant is what makes that expressible:
# ``None`` already means "the plain assistant", so without a distinct value a held item would
# either fall through to the plain assistant or be handed to whichever character happens to be on
# duty. Both are the same mistake as running it on the wrong day. The person chose WHO does this
# work; the only thing the calendar decides is WHEN.
#
# The item that still names a rep is an ADOPTED RECURRING TASK (``assignee_rep_id`` on the task the
# person scheduled). Goals no longer name one at all: they are context for a run, never an
# assignment to one, so nothing about a goal can put a character on duty or take one off.
PERSONA_HELD = _HeldPersona()


# What the persona resolvers can answer: a rep id, ``None`` (the plain assistant), or
# ``PERSONA_HELD`` (nobody today -- this work waits for the day its character is rostered for).
ResolvedPersona = Union[str, None, _HeldPersona]


def _rostered_rep_ids(autopilot_cfg: Dict[str, Any]) -> set:
    """Every rep_id the quest's roster names, ``instructions_only`` entries included.

    Appearing in the roster at all is what puts a character under the day rule: the person opened
    that entry and set their days. A character with NO entry has no day setting to follow, so work
    assigned to them is not held on any day.
    """
    return {str(p.get("rep_id")) for p in (autopilot_cfg.get("personas") or []) if p.get("rep_id")}


def _work_routing_entries(autopilot_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The roster entries work routing can use: every entry not flagged ``instructions_only``.

    A roster made up only of ``instructions_only`` entries is, for routing, no roster at all (the
    flag says exactly that: behave as though this character were not in the roster). That is what
    keeps the day rule from turning an unassigned recurring task into work nobody may do.
    """
    return [p for p in (autopilot_cfg.get("personas") or [])
            if not _truthy(p.get("instructions_only"))]


def resolve_persona(autopilot_cfg: Dict[str, Any], now: datetime,
                    fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                    fallback_context: Optional[Dict[str, Any]] = None) -> ResolvedPersona:
    """Which character this quest routes UNNAMED work to TODAY, or ``PERSONA_HELD`` when the honest
    answer is "nobody, today".

    THE DAY RULE COMES FIRST AND NOTHING OVERRIDES IT: a character works a quest only on the days
    their own roster entry names. Everything below decides how work is routed AMONG the characters
    the person rostered for today; none of it can reach past that set.

      1. A quest ``autopilot.personas`` entry whose ``days`` include today, EXCLUDING entries
         flagged ``instructions_only`` (checked first, so an explicit day-restricted entry wins
         over an unrestricted one for the same day).
      2. A quest ``autopilot.personas`` entry with NO ``days`` restriction, again excluding
         ``instructions_only`` entries (applies any day).
      3. ``PERSONA_HELD`` when the roster DOES name work-routing characters but none of them is on
         duty today. Their work waits; handing one character's work to whoever happens to be around
         is its own wrong answer, and falling through to the plain assistant is the bug that made
         this rule necessary (a quest with standing instructions ran as the plain assistant on days
         nobody was rostered for).
      4. A consumer-injected fallback (e.g. the existing card-vote resolver), given
         ``fallback_context`` (the item being routed, or ``{}`` when there is none).
      5. ``None`` -- the plain assistant persona (no character voice).

    An entry naming DEFAULT_ASSISTANT_REP_ID is on duty and takes work by rules 1 and 2 like any
    other, but resolves to steps 4/5 rather than to itself: it is the person writing the fallback
    down so it can carry days and a brief, not a character anybody can look up.

    Steps 4 and 5 are reachable ONLY when the roster names no work-routing character at all, which
    is every quest that never configured one. Those quests behave exactly as they always have.

    There is no step reading an ``assignee_rep_id`` off a goal any more, because a goal no longer
    carries one: goals are context for a run and never an assignment to one. The one item that
    still names a rep is an adopted recurring task, and ``resolve_task_persona`` below is where
    that reading lives, so the naming is beside the only input that still exists for it.

    Why ``instructions_only`` is excluded from 1 and 2: a roster entry is two different statements
    at once ("this character is on duty today" and "this character is who unnamed work goes to"),
    and rule 1 beating rule 2 makes the second statement greedy. A quest with a weekday worker and
    a Saturday specialist would read, on Saturday, as "the specialist takes everything" -- so the
    specialist inherits work they were never meant to touch. ``instructions_only`` says this entry
    is only the FIRST statement: the character is on duty and works their own standing
    instructions, and routing behaves as though they were not in the roster at all.
    """
    def unnamed() -> ResolvedPersona:
        """Steps 4 and 5: the consumer's fallback, else the plain assistant."""
        if fallback_resolver is not None:
            try:
                resolved = fallback_resolver(dict(fallback_context or {}))
                if resolved:
                    return str(resolved)
            except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
                log.info("autopilot: persona fallback_resolver raised; treating as no match",
                         exc_info=True)
        return None

    routing_entries = _work_routing_entries(autopilot_cfg)
    today = _weekday_abbrev(now)
    for persona in routing_entries:
        days = persona.get("days")
        if days and today in days:
            rep_id = persona.get("rep_id")
            if rep_id:
                # The default-assistant entry is on duty today and takes the work, but it names
                # no character: routing to it IS routing to nobody in particular, so it answers
                # with the same plain assistant an unrostered quest gets. Returning the marker
                # would hand a consumer an id it cannot look up.
                return unnamed() if str(rep_id) == DEFAULT_ASSISTANT_REP_ID else str(rep_id)
    for persona in routing_entries:
        if not persona.get("days"):
            rep_id = persona.get("rep_id")
            if rep_id:
                return unnamed() if str(rep_id) == DEFAULT_ASSISTANT_REP_ID else str(rep_id)
    if any(p.get("rep_id") for p in routing_entries):
        # Including a roster of nothing but the default assistant on days it does not name: the
        # person said which days this quest runs, and "held" is what honouring that looks like.
        return PERSONA_HELD
    return unnamed()


def resolve_task_persona(task: Dict[str, Any], autopilot_cfg: Dict[str, Any], now: datetime,
                         fallback_resolver: Optional[Callable[[Dict[str, Any]],
                                                              Optional[str]]] = None
                         ) -> ResolvedPersona:
    """Who works an ADOPTED RECURRING TASK today, under the same day rule.

    A recurring task the person scheduled can name a character in its own ``assignee_rep_id``, and
    that naming is honoured FIRST: it is a human choosing who does this specific thing, which is
    more specific than any roster-wide preference. What it cannot do is beat that character's DAYS,
    which is a statement by the same human about when they work this quest at all. So:

      * named character on duty today, or carrying no roster entry at all (no entry means the
        person set no days for them, so there is no day setting to follow) -> that character;
      * named character rostered but NOT on duty today -> ``PERSONA_HELD``, and the caller leaves
        the occurrence queued to run on its own rather than re-assigning it to whoever is around;
      * no name on the task -> whatever ``resolve_persona`` routes for this quest today, so an
        unassigned occurrence rides along with the character already working the quest instead of
        spawning a second run.
    """
    assignee = task.get("assignee_rep_id")
    if assignee:
        assignee = str(assignee)
        on_duty = {str(entry.get("rep_id"))
                   for entry in persona_entries_on_duty(autopilot_cfg, now)
                   if entry.get("rep_id")}
        if assignee in on_duty or assignee not in _rostered_rep_ids(autopilot_cfg):
            return assignee
        return PERSONA_HELD
    return resolve_persona(autopilot_cfg, now, fallback_resolver, fallback_context=task)


def persona_entries_on_duty(autopilot_cfg: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """The full roster ENTRIES that apply TODAY, most specific first, one per rep_id.

    Day-restricted entries that name today come before unrestricted ones, matching
    ``resolve_persona``'s precedence: an explicit "Bailey on Mondays" outranks a catch-all. A
    rep_id appearing in several entries is kept once, at its earliest (most specific) position.

    The entries, not just the ids, because an entry now carries the character's own standing
    ``instructions`` for this quest and its ``instructions_only`` flag, and a caller deciding
    whether that character has a job to do today needs to read them. ``personas_on_duty`` stays
    the answer to the narrower "who is on duty" question and is written in terms of this.
    """
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for restricted in (True, False):
        for persona in personas:
            days = persona.get("days")
            if bool(days) is not restricted:
                continue
            if days and today not in days:
                continue
            rep_id = persona.get("rep_id")
            if rep_id and str(rep_id) not in seen:
                seen.add(str(rep_id))
                entries.append(persona)
    return entries


def personas_on_duty(autopilot_cfg: Dict[str, Any], now: datetime) -> List[str]:
    """Every rep_id in a quest's roster that applies TODAY, most specific first.

    Day-restricted entries that name today come before unrestricted ones, matching
    ``resolve_persona``'s precedence: an explicit "Bailey on Mondays" outranks a catch-all.

    The DEFAULT-ASSISTANT marker is filtered out: it names no character, so a quest rostered only
    to it answers "nobody in particular", which is what an attended session should hear.

    This is what lets an ATTENDED session speak as the same character that would work the quest
    autonomously. Opening a chat inside a quest and getting a generic assistant, while its
    autopilot runs as a named character with that character's accumulated corrections, makes the
    two feel like unrelated systems when they are meant to be one.

    ``instructions_only`` entries are deliberately still listed here. That flag says a character
    does not take the quest's GOALS, not that they are absent: they are on duty, working their own
    standing instructions, and a person opening a chat on the quest should be able to reach them.
    """
    return [str(entry.get("rep_id")) for entry in persona_entries_on_duty(autopilot_cfg, now)
            if str(entry.get("rep_id")) != DEFAULT_ASSISTANT_REP_ID]


def persona_instructions_for(autopilot_cfg: Dict[str, Any], rep_id: Optional[str]) -> Optional[str]:
    """A character's own standing instructions for this quest, or None.

    The WHOLE roster is searched, not only today's on-duty entries. One character can hold
    several entries -- a day-restricted one carrying their instructions plus a catch-all that is
    what actually puts them on duty today -- and a character working this quest without their
    standing brief would be a different character than the one the person configured. The first
    entry for that rep with non-empty instructions wins, so a roster that lists the same character
    twice is read the same way ``persona_entries_on_duty`` reads it. (Since the day rule landed,
    every character this quest routes work to today is on duty today; searching the whole roster
    is what makes WHICH of their entries carries the text irrelevant.)

    Truncated defensively at ``MAX_INSTRUCTIONS_CHARS``, the same cap and the same warning the
    quest-level field gets. As there, the text itself is never logged: it is the person's private
    content and can run to 8 KB.
    """
    if not rep_id:
        return None
    wanted = str(rep_id)
    for entry in (autopilot_cfg.get("personas") or []):
        if str(entry.get("rep_id") or "") != wanted:
            continue
        instructions = str(entry.get("instructions") or "").strip()
        if not instructions:
            continue
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            log.warning("autopilot: persona %s instructions truncated from %d to %d characters",
                        wanted, len(instructions), MAX_INSTRUCTIONS_CHARS)
            instructions = instructions[:MAX_INSTRUCTIONS_CHARS]
        return instructions
    return None


def default_quest_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The QUEST-WIDE brief a batch works to when the quest itself carries none: how to work here
    and how to deliver, for every character on the quest.

    The SERVER'S value wins whenever the payload carries one:
    ``autopilot.default_quest_instructions`` is derived read-only by quest-backend, so the text a
    run works to is whatever that backend currently says it is, and improving it there improves
    every quest that never wrote its own. ``BUNDLED_DEFAULT_QUEST_INSTRUCTIONS`` is only what an
    older backend (or any client that does not serve the field) degrades to, so a character on duty
    still has a specification instead of a silent pass. See that constant for why the copy exists
    at all.
    """
    served = str(autopilot_cfg.get("default_quest_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_QUEST_INSTRUCTIONS


def default_persona_instructions_for(autopilot_cfg: Dict[str, Any]) -> str:
    """The PERSONA brief a batch works to when that character carries none: what to work on.

    Resolved exactly like ``default_quest_instructions_for`` and from its own served field
    (``autopilot.default_persona_instructions``), because the two slots default independently: a
    quest that wrote a quest-wide brief and no character brief still needs this one, and the
    reverse is just as common.
    """
    served = str(autopilot_cfg.get("default_persona_instructions") or "").strip()
    return served or BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS


def split_held_for_another_day(tasks: List[Dict[str, Any]], autopilot_cfg: Dict[str, Any],
                              now: datetime
                              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split adopted recurring tasks into ``(workable today, held)`` under the day rule.

    Held means the task names a character the person rostered for other days, or that the roster's
    work-routing characters are all off duty today: either way nobody may work it today, and it
    comes back on a day one of them does. Held is not lost. The caller simply does not ADOPT a held
    occurrence, so it stays queued and runs as the person scheduled it, which is the same direction
    ``_close_adopted`` fails in (duplicated work is recoverable, lost work is not).

    Deliberately called BEFORE anything is composed for these tasks, so a held occurrence becomes
    no part of this run: nothing this character is asked to do, and no close on a task somebody
    else's day owns. The incident this rule comes from was a character working another's item on
    the wrong day, and that is what stays impossible.

    The fallback resolver is not consulted here: it is only ever reached for a quest whose roster
    names no work-routing character, and such a quest can hold nothing, so passing it would spend a
    consumer callback on a decision it cannot change.
    """
    workable: List[Dict[str, Any]] = []
    held: List[Dict[str, Any]] = []
    for task in tasks:
        if resolve_task_persona(task, autopilot_cfg, now) is PERSONA_HELD:
            held.append(task)
        else:
            workable.append(task)
    return workable, held
