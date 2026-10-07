"""THE REACH JUDGE: one small question, answered by a stronger model, on every routing decision.

This is the cheap way to use an expensive model: not to make the decision, and not to review it
afterwards, but to settle the ONE sub-judgment the cheap model cannot make, on a prompt of a few
hundred tokens, and hand the answer to the cheap planner as a fact.

Measured, not assumed (2026-10-05, 765 labelled messages):

  * A small model routes most requests well but fails one class almost completely. Asked to do
    something that lives outside what a read can reach (a machine, a repository, a document held
    elsewhere), it acknowledges the rule in its own rationale and issues the forbidden read anyway,
    with an invented path or scope, roughly four times in five. Three prompt rewrites moved that
    inside the noise floor. It is a capability gap, not a wording gap.
  * Reviewing the decision afterwards does not fix it. A stronger model shown the decision and a
    digest of the grounding escalated 44 percent of decisions and scored WORSE than no review at
    all (52 percent against 60 percent on the hand-off groups), because a reviewer handed a
    decision argues with it, while the thing that was actually missing was a fact.
  * Asking the stronger model the reach question on its OWN, with nothing but the request, a short
    statement of what a read can reach, and the attached environments, is 94.7 percent accurate on
    95 labelled dev cases, including 19 of 19 and 6 of 6 on the two groups the cheap model fails,
    and 15 of 15 on questions about the world. At about 400 input tokens that is a fraction of one
    planner call.

So the judge runs once per turn, before planning, and its verdict is stamped into the planner's
prompt as something already settled. Three verdicts, and only two of them change anything:

  "inside"  nothing is added; the planner plans exactly as it would have.
  "outside" the planner is told the work is out of a read's reach, and whether an attached
            environment covers it, so the choice is hand it off or say plainly that nothing can.
  "world"   the planner is told this is a current public fact, so it answers rather than searching
            its own sources or handing the question to a machine.

It is OFF by default and needs one piece of consumer configuration, ``read_reach_summary``: the
short statement of what this deployment's reads can reach and what its environments handle. The
library cannot know that, and a judge given a generic guess would be worse than no judge.

Generic: no org, product, or deployment names.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: The verdicts the judge may return. Anything else is treated as "no verdict" and changes nothing.
REACH_VERDICTS = ("inside", "outside", "world")

REACH_JUDGE_TOOL: Dict[str, Any] = {
    "name": "reach",
    "description": "Record where what this request needs actually lives.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reach": {"type": "string", "enum": list(REACH_VERDICTS)},
            "covered_by": {
                "type": ["string", "null"],
                "description": "With 'outside': the name of the environment whose description "
                               "covers that kind of work on that place, or null if none does.",
            },
        },
        "required": ["reach"],
    },
}

REACH_JUDGE_PROMPT = """\
Decide ONE thing about the request below, and nothing else. Do not answer it and do not plan it.

WHAT THIS ASSISTANT CAN REACH:
{reach_summary}

THE REQUEST:
{message}

"outside": doing or answering this needs a machine or server and its files, folders, processes,
  jobs, logs or quotas; a code repository; or a document, spreadsheet, drive or service held
  somewhere other than the readable sources above. Set "covered_by" to the environment whose
  description covers that kind of work on that place, or null when none of them does.
"world": it asks for a current public fact about the world (news, a price, the weather, a result,
  what is happening now). General knowledge the assistant simply knows is NOT this.
"inside": everything it needs is in the readable sources above.
{web_note}
JUDGE WHERE THE ANSWER LIVES, NOT WHERE THE WORK HAPPENED. A request about someone's own or their
team's work, plans, goals, tasks, records or progress is "inside" even when the work it describes
is carried out elsewhere: the answer is in the records above. "How is that piece of work going",
"what is the team on this week", "what is still open", "remind me to do X" are all "inside". It is
"outside" only when the request needs you to INSPECT OR CHANGE the other place itself. A request
that merely RECORDS a fact about another place into these records is also "inside".
"""

#: What the planner is told when the work is out of reach AND something can do it.
OUTSIDE_COVERED_LINE = (
    "What this request needs does NOT live in any source you can read: it lives on a machine, in a "
    "repository, or in a document or service held elsewhere. This was judged separately and is "
    "settled, so do not read, grep, query or run discovery for it, and do not invent a scope or a "
    "path. The attached environment \"{covered_by}\" handles that kind of work. Hand it off: a "
    "short answer saying what you are handing over, with deferred_deep carrying the work. If the "
    "request also has a part your own sources DO cover, answer that part in the same reply."
)
#: What the planner is told when the work is out of reach and nothing can do it.
OUTSIDE_UNCOVERED_LINE = (
    "What this request needs does NOT live in any source you can read, and nothing attached can "
    "reach it either. This was judged separately and is settled, so do not read, grep, query or "
    "run discovery for it, do not invent a scope or a path, and do not hand it off to anything. "
    "Answer, and say plainly that you cannot reach it from here."
)
#: Same verdict, but a live web adapter IS wired: a private place nothing attached can reach is
#: still unreachable, but if the thing is actually public on the web, a web read can fetch it.
OUTSIDE_UNCOVERED_LINE_WEB = (
    "What this request needs does NOT live in any source you can read, and nothing attached can "
    "reach it either. This was judged separately and is settled, so do not invent a scope or a "
    "path, and do not hand it off to anything. If it is a private place, say plainly that you "
    "cannot reach it. If the thing is actually public on the web, issue a {\"web\": ...} or "
    "{\"web_page\": ...} read for it instead."
)
#: What the planner is told when the request is about the world right now, and no live web
#: adapter is wired: the old behaviour, answer from what you know and say so plainly.
WORLD_LINE = (
    "This request asks for a current public fact about the world. That is not work for a machine "
    "and not in any source you can read. This was judged separately and is settled: answer it. "
    "Say what you reliably know, and say plainly that you cannot check a live source for the "
    "current value. Do not hand it off and do not search your own sources for it."
)
#: Same verdict, but a live web adapter IS wired (``Orchestrator.web``): the fact is reachable,
#: so the planner is told to read it rather than answer from memory or hand it off.
WORLD_LINE_WEB = (
    "This request asks for a current public fact about the world. That is not work for a machine "
    "and not in any source you can read, but it IS reachable: this was judged separately and is "
    "settled, so issue a {\"web\": \"<query>\"} read for it instead of answering from memory or "
    "handing it off."
)

VERDICT_HEADING = "--- WHERE WHAT THIS REQUEST NEEDS ACTUALLY LIVES (already settled) ---\n"

#: Appended to ``REACH_JUDGE_PROMPT`` when a live web adapter is wired (``Orchestrator.web``), so
#: the judge itself knows a public web page or document is reachable, not just the verdict text
#: stamped into the planner afterwards. Default "" leaves the prompt byte-for-byte unchanged.
WEB_REACH_NOTE = (
    "A LIVE WEB READ IS AVAILABLE: a current public fact or a public web page or document "
    "counts as \"world\". Private data, local files, machines, private repositories and "
    "logged-in services stay \"inside\" or \"outside\"."
)


def normalize_verdict(raw: Any) -> Optional[Dict[str, Any]]:
    """A judge response reduced to ``{"reach": ..., "covered_by": ...}``, or None.

    None means "no usable verdict", and the planner then runs exactly as it would have with no
    judge at all. Every failure path leads here on purpose: an unparseable answer, a hallucinated
    verdict, a wrong type. A pre-stage that cannot fail open is a pre-stage that can take the turn
    down, and this one is an optimisation, never a dependency.
    """
    if not isinstance(raw, dict):
        return None
    reach = raw.get("reach")
    if not (isinstance(reach, str) and reach.strip().lower() in REACH_VERDICTS):
        return None
    covered = raw.get("covered_by")
    covered = covered.strip() if isinstance(covered, str) and covered.strip() else None
    if covered and covered.lower() in ("null", "none", "n/a"):
        covered = None
    return {"reach": reach.strip().lower(), "covered_by": covered}


def judge_prompt(message: str, reach_summary: str, max_message_chars: int = 2000, *,
                 web_configured: bool = False) -> str:
    """The judge's own prompt. ``web_configured=False`` (the default) is byte-for-byte unchanged
    from before a web adapter existed; ``True`` adds ``WEB_REACH_NOTE`` so the judge itself, not
    only the verdict text stamped into the planner afterwards, knows a live web read exists.

    The note gets the same blank-line paragraph framing as every other section of this prompt
    (a leading and trailing blank line); with no note the template collapses back to the single
    blank line the unchanged, web-off rendering has always had.
    """
    return REACH_JUDGE_PROMPT.format(
        reach_summary=reach_summary.strip(), message=(message or "")[:max_message_chars],
        web_note=("\n" + WEB_REACH_NOTE + "\n") if web_configured else "")


def verdict_block(verdict: Optional[Dict[str, Any]], *, web_configured: bool = False) -> str:
    """The block appended to the planner prompt, or "" when nothing should change.

    An "inside" verdict deliberately adds NOTHING. It is the common case, so saying "this is
    reachable" on every ordinary request would spend tokens to tell the planner what it already
    assumes, and would give it a sentence to over-read on the requests that are genuinely mixed.

    ``web_configured`` (default False, so an existing caller is byte-for-byte unchanged) is
    whether a live web adapter is wired (``Orchestrator.web``). It changes the "world" wording
    (with no web adapter the planner is told to answer from what it knows, ``WORLD_LINE``,
    unchanged; with one wired, a current public fact is reachable through a read, not a hand-off
    or a guess, ``WORLD_LINE_WEB``) and the uncovered "outside" wording (``OUTSIDE_UNCOVERED_LINE``
    unchanged versus ``OUTSIDE_UNCOVERED_LINE_WEB``, which still says a private place stays
    unreachable but points a genuinely public one at a web read instead of a hand-off). A
    structural flag, never a keyword check on model output.
    """
    if not verdict:
        return ""
    reach = verdict.get("reach")
    if reach == "world":
        return VERDICT_HEADING + (WORLD_LINE_WEB if web_configured else WORLD_LINE) + "\n"
    if reach != "outside":
        return ""
    covered = verdict.get("covered_by")
    if covered:
        return VERDICT_HEADING + OUTSIDE_COVERED_LINE.format(covered_by=covered[:120]) + "\n"
    return VERDICT_HEADING + (OUTSIDE_UNCOVERED_LINE_WEB if web_configured
                              else OUTSIDE_UNCOVERED_LINE) + "\n"


def parse_judge_text(text: Any) -> Optional[Dict[str, Any]]:
    """Normalize a judge answer that arrived as text rather than a parsed object."""
    if isinstance(text, dict):
        return normalize_verdict(text)
    if not isinstance(text, str):
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[1] if "\n" in body else body
    try:
        return normalize_verdict(json.loads(body))
    except Exception:  # noqa: BLE001
        return None
