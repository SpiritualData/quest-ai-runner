"""Words a person reads carry names, never record ids or machine headers.

An autopilot's standing instructions open with machine-read lines (``SOLUTIONS YOU ITERATE: esr_...``) and its records carry keys
(``quest_...``, ``rep_...``); a model that echoes them into an email, task, note or ask hands the reader something unreadable
(2026-11-16: a subscribers email went out titled with that line and an id). Like ``count_em_dashes`` this never rewrites the text, because only
the writer can name a record the way a person would. Unlike it, the outward write tools REFUSE with the offending strings named, because the writer
is a model that is still in the run and can rewrite at once. Ids inside a URL (a real link) are fine. Pure; the patterns are id syntax and the
fixed machine header labels, never a judgement about meaning.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

#: The one statement of the rule: the brief of every autopilot run holds it, and a refusal repeats its point.
READABLE_NAMES_RULE = (
    "NAMES, NEVER IDS. The first lines of your standing instructions (for example \"SOLUTIONS YOU ITERATE: <ids>\") and any id such as esr_..., "
    "rep_..., quest_..., goal_..., int_..., task_... or rep-rep-... are machine keys: use them only to find records. In anything a person reads "
    "(email subject and body, task titles and descriptions, goal updates, quest notes, asks, your result) write the proper names of the people, "
    "goals, quests, solutions and metrics, never an id and never a machine header line. A link is a full URL, with readable link text where the "
    "medium allows."
)

#: A record id: a lowercase prefix, an underscore, 8 or more hex digits (esr_b451d0cb3cec), or the doubled persona form (rep-rep-...).
ID_RE = re.compile(r"\b(?:[a-z]{2,10}_(?=[0-9a-f]*\d)[0-9a-f]{8,}|rep-rep-[0-9a-z]{4,})\b", re.IGNORECASE)
#: A real link: ids inside it are part of the address and stay.
URL_RE = re.compile(r"https?://[^\s<>)\]]+|www\.[^\s<>)\]]+", re.IGNORECASE)
#: The upper-case labels of machine-read lines that standing instructions carry, anywhere in a line (the 2026-11-16 subject held one after a prefix).
HEADER_RE = re.compile(r"\b(SOLUTIONS YOU ITERATE|ESOI READINESS|STANDING WORK STANDARD)\b\**\s*:")


def machine_strings(*texts: Optional[str]) -> List[str]:
    """The record ids and machine header labels in ``texts``, each once, in order of appearance (URLs excluded)."""
    found: List[str] = []
    for text in texts:
        plain = URL_RE.sub(" ", str(text or ""))
        for match in HEADER_RE.finditer(plain):
            found.append(match.group(1) + ":")
        found.extend(m.group(0) for m in ID_RE.finditer(plain))
    return list(dict.fromkeys(found))


def readable_reason(*texts: Optional[str]) -> Optional[str]:
    """Why these texts may not go out to a person, or None when they read as names. A plain instruction for the writer, naming each offender."""
    found = machine_strings(*texts)
    if not found:
        return None
    shown = ", ".join(repr(s) for s in found[:6]) + (" and more" if len(found) > 6 else "")
    return (f"it holds machine text a person cannot read ({shown}). Look each one up and write the record's own name instead "
            f"(the person's, goal's, quest's, solution's or metric's title), and leave out header lines such as SOLUTIONS YOU ITERATE")


def refusal(what: str, *texts: Optional[str]) -> Optional[str]:
    """The tool reply refusing ``what`` (\"The email was not sent\"), or None when the texts are readable."""
    reason = readable_reason(*texts)
    return f"{what}: {reason}. Rewrite it with names and send it again." if reason else None

