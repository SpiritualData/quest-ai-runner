"""What a finished turn is allowed to TEACH the user's context cards.

A context card is DURABLE: a fact written onto one grounds every later turn that selects it, so a
card that records something the turn merely CLAIMED turns one wrong sentence into a standing belief.
Two live examples from one evening's eval run:

  * a card read "Goal added: <a goal> for the period <a period>" although the turn's generated
    program had run a write that matched no document, and the database diff showed no change;
  * a card read "Definitive summary of <a person>'s request and project requirements ..." although
    the turn's reads had returned no match and the reply was invented. A later turn's prompt then
    carried that invention as context.

Both were written by the end-of-turn card updater from the REPLY TEXT, which is the one thing in a
turn that is never evidence of anything.

THE RULE: a card may record what the turn OBSERVED, never what it CLAIMED.

An observation is structural, reported by code, never parsed out of a model's prose:

  * a deep run's own receipts (``DeepResult.observations``). ``DeepResult.observations_reported``
    marks a runner that records them, which is what makes an EMPTY list meaningful: it then means
    the run observed nothing, rather than that the runner cannot tell. A runner that cannot tell
    leaves the flag False and this module leaves its turn exactly as it behaved before.
  * ``DeepResult.changed_nothing``: the run's write receipts show no change landed. A card may then
    not say anything was created, added, updated, logged or deleted, because nothing was.
  * this turn's own reads that actually returned content (``read_observation_lines`` over the
    orchestrator's ``gathered``). A read that returned nothing supports no fact.

And an UNVERIFIED turn teaches nothing: ``DeepResult.met`` is the goal loop's structured verdict,
false both for a verified not-met and for a run whose verification could not run at all. A run that
handed the work to a human decision is not a verdict against it (see
``run_verdict_allows_learning``), but it still has to have observed something.

When the structured facts do not support learning, the updater's edit plan is NARROWED to its
REMOVALS (see ``narrow_edits_to_removals``). Dropping a wrong statement from a card is always safe
and is the one correction a turn with no evidence can still make; adding, replacing or renaming is
not. Nothing here inspects the WORDS of a model's output (CLAUDE.md hard rule #3), and nothing here
asks a model to police itself in prose: the absence of support is what drops the edit.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# Bounds for the observations rendered into the updater's prompt. The block is MATERIAL (the only
# thing a new card fact may come from), so it carries a real excerpt of each read rather than just
# a count, but it shares one cheap call's prompt with the request, the executed work and the user's
# current cards, so it is bounded on every axis.
DEFAULT_MAX_OBSERVATION_LINES = 12
DEFAULT_MAX_OBSERVATION_CHARS = 200
DEFAULT_MAX_BLOCK_CHARS = 1500

# The heading the observations are rendered under in the card-updater prompt.
OBSERVATIONS_HEADING = ("--- WHAT THIS TURN ACTUALLY OBSERVED (receipts of what landed, and reads "
                        "that returned content) ---")
# What the block says when the turn observed nothing at all.
NO_OBSERVATIONS_TEXT = ("(nothing: no change landed and no read returned content this turn, so "
                        "this turn has no fact to record)")


def one_line(value: Any, limit: int = DEFAULT_MAX_OBSERVATION_CHARS) -> str:
    """Collapse ``value`` to one whitespace-normalized line of at most ``limit`` characters."""
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def read_observation_lines(gathered: Optional[Iterable[Any]],
                           *, max_lines: int = DEFAULT_MAX_OBSERVATION_LINES,
                           max_chars: int = DEFAULT_MAX_OBSERVATION_CHARS) -> List[str]:
    """One line per read THIS TURN that actually returned content.

    Takes the orchestrator's ``gathered`` observations (``{"kind": "grep"|"read"|"query"|"error",
    ...}``) and keeps only the ones carrying real content: a grep with at least one hit, a read or
    query with non-empty text. A capability/source MENU (``discovery``), a note about the turn
    itself (``planner_only``) and a gather error are not content and never appear. Each kept line
    names WHERE the content came from and carries a bounded excerpt of it, because this is the
    material a card fact may be drawn from. Never raises.
    """
    lines: List[str] = []
    try:
        for obs in (gathered or []):
            if len(lines) >= max_lines:
                break
            if not isinstance(obs, dict):
                continue
            if obs.get("discovery") or obs.get("planner_only"):
                continue
            kind = obs.get("kind")
            if kind == "grep":
                hits = [h for h in (obs.get("hits") or []) if isinstance(h, dict)]
                if not hits:
                    continue
                scope = obs.get("scope")
                where = f" in {scope}" if scope else ""
                first = one_line(hits[0].get("line"), max_chars)
                lines.append(f"GREP {obs.get('pattern')!r}{where} returned {len(hits)} hit(s), "
                             f"first: {first}")
            elif kind in ("read", "query"):
                text = (obs.get("text") or "").strip()
                if not text:
                    continue
                where = obs.get("rel_path") or obs.get("locator") or ""
                head = f"{str(kind).upper()} {where}".strip()
                lines.append(f"{head}: {one_line(text, max_chars)}")
    except Exception:  # noqa: BLE001 — evidence collection must never break a turn
        return lines
    return lines


def deep_observation_lines(results: Optional[Iterable[Any]]) -> List[str]:
    """Every line the turn's deep runs reported as observed, in order. Never raises.

    These come from ``DeepResult.observations``, which a runner fills from its OWN records (a write
    receipt for a change that provably landed, a source it really read back), never from the text
    of its output.
    """
    lines: List[str] = []
    try:
        for res in (results or []):
            for line in (getattr(res, "observations", None) or []):
                text = one_line(line, 0)
                if text:
                    lines.append(text)
    except Exception:  # noqa: BLE001
        return lines
    return lines


def turn_observations(results: Optional[Iterable[Any]],
                      gathered: Optional[Iterable[Any]]) -> List[str]:
    """All of this turn's observations: the deep runs' receipts first, then the reads that returned
    content. This is the ONLY material the card updater may draw a new fact from."""
    return deep_observation_lines(results) + read_observation_lines(gathered)


def observations_reported(results: Optional[Iterable[Any]]) -> bool:
    """True when at least one of the turn's deep runs RECORDS what it observed.

    Without this, an empty observation list is ambiguous: it could mean the run observed nothing, or
    that the runner has no way to tell. Only when a runner declares it records them does absence of
    an observation mean absence of the observation, so only then may this module drop an edit for
    lack of support. Never raises."""
    try:
        return any(bool(getattr(res, "observations_reported", False)) for res in (results or []))
    except Exception:  # noqa: BLE001
        return False


def run_verdict_allows_learning(result: Any) -> bool:
    """True when ONE deep run's outcome is not a verdict against it.

    ``DeepResult.met`` is the goal loop's own structured verdict: it is false for a verified not-met
    AND for a run whose verification could not run (an LLM outage, no verify tier, a parse failure),
    which is exactly the unverified case that must teach nothing.

    The one exception is a run that handed the work to a HUMAN DECISION (``decision_id``): nothing
    was judged and nothing failed there, the work is waiting on a person. Such a run may still teach
    what it observed on the way, and its own receipts are what say that nothing has changed yet.
    """
    try:
        if bool(getattr(result, "met", False)):
            return True
        return bool(getattr(result, "decision_id", None))
    except Exception:  # noqa: BLE001
        return False


def verdict_allows_learning(results: Optional[Iterable[Any]]) -> bool:
    """True when no deep run of this turn carries a verdict against it (see
    ``run_verdict_allows_learning``). A turn with no deep run at all has no run to learn from here.
    Never raises."""
    try:
        runs = list(results or [])
        return bool(runs) and all(run_verdict_allows_learning(res) for res in runs)
    except Exception:  # noqa: BLE001
        return False


def changed_nothing(results: Optional[Iterable[Any]]) -> bool:
    """True when any deep run of this turn reports that its write receipts show no change landed."""
    try:
        return any(bool(getattr(res, "changed_nothing", False)) for res in (results or []))
    except Exception:  # noqa: BLE001
        return False


def turn_teaches_new_facts(results: Optional[Iterable[Any]],
                           gathered: Optional[Iterable[Any]] = None) -> bool:
    """Whether this turn may teach the user's cards a NEW fact at all.

    False when any of the following structured facts holds:
      * a deep run was not verified met (a not-met or an unverified turn teaches nothing);
      * a deep run's write receipts show nothing landed (``changed_nothing``), so no card may say
        something was created, added, updated, logged or deleted;
      * the runs RECORD their observations and the turn has none, neither a receipt nor a read that
        returned content, so there is nothing a fact could come from.

    When no run records its observations the last test is skipped and the turn behaves exactly as it
    did before this gate existed (a runner that cannot tell is not evidence that nothing happened).
    Never raises.
    """
    if not verdict_allows_learning(results):
        return False
    if changed_nothing(results):
        return False
    if observations_reported(results) and not turn_observations(results, gathered):
        return False
    return True


def narrow_edits_to_removals(edits: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """The part of a card-updater edit plan a turn with no supporting observation may still apply:
    its REMOVALS.

    Dropping an item a card should not be carrying needs no evidence from this turn, so a correction
    that only removes a wrong statement stays allowed. Everything that would record something (new
    content items, replacements, a new name or description, and so a whole new card) is dropped.
    Returns a NEW list of new dicts; never mutates the caller's edits, never raises.
    """
    kept: List[Dict[str, Any]] = []
    try:
        for edit in (edits or []):
            if not isinstance(edit, dict):
                continue
            card_id = str(edit.get("card_id") or "").strip()
            removals = [str(r) for r in (edit.get("remove") or []) if isinstance(r, (str, int))]
            if card_id and removals:
                kept.append({"card_id": card_id, "remove": removals})
    except Exception:  # noqa: BLE001
        return kept
    return kept


def render_observations_block(lines: Optional[Iterable[str]],
                              *, max_chars: int = DEFAULT_MAX_BLOCK_CHARS) -> str:
    """The observations as the prompt sees them: one bullet per observation, bounded in total size.

    Keeps the EARLIEST lines when it has to cut, because the deep runs' receipts come first and a
    receipt is the most authoritative thing a turn has. Returns ``NO_OBSERVATIONS_TEXT`` when there
    is nothing, so the prompt always states the fact rather than leaving a blank the model can fill
    with its own assumption. Never raises.
    """
    try:
        bullets: List[str] = []
        used = 0
        for line in (lines or []):
            text = one_line(line, 0)
            if not text:
                continue
            bullet = f"- {text}"
            if max_chars and used + len(bullet) + 1 > max_chars:
                bullets.append("- (more observations omitted)")
                break
            bullets.append(bullet)
            used += len(bullet) + 1
        return "\n".join(bullets) if bullets else NO_OBSERVATIONS_TEXT
    except Exception:  # noqa: BLE001
        return NO_OBSERVATIONS_TEXT
