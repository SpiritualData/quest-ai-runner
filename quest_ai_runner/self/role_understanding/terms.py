"""The names QAR knows about itself, and the names a request holds that nothing has placed yet.

QAR's own glossary (``glossary.csv`` beside this module) adds to the hub's (AHOHS, ESOI). ``ground`` places every name a request holds from the glossaries, then
from the files around the folders this deployment works in (the hub's bounded search), and says which names nothing placed. A search that found nothing is
remembered for a few minutes so a turn does not pay for the same search again. The hub does the placing; this module only supplies QAR's names and folders."""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from hubofhubs.api import ground_terms, term_lines, ungrounded

GLOSSARY = Path(__file__).resolve().parent / "glossary.csv"
REMEMBER_SECONDS = 600.0
remembered: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}


def glossary() -> List[Dict[str, str]]:
    try:
        with open(GLOSSARY, encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def ground(request: str, folders: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """One record per name in the request (the hub's ``ground_terms`` shape). The first folder whose files place a name wins; a name no folder placed stays ungrounded."""
    roots = [f for f in folders if f and Path(f).is_dir()] or [os.getcwd()]
    key = (request, "|".join(roots))
    now = time.monotonic()
    hit = remembered.get(key)
    if hit is not None and now - hit[0] < REMEMBER_SECONDS:
        return [dict(t) for t in hit[1]]
    merged: Dict[str, Dict[str, Any]] = {}
    for index, root in enumerate(roots[:3]):
        for term in ground_terms(request, root, glossary(), search=True):
            if term["term"] not in merged or (term["grounded"] and not merged[term["term"]]["grounded"]):
                merged[term["term"]] = term
    found = list(merged.values())
    remembered[key] = (now, [dict(t) for t in found])
    return found


def view_block(terms: Sequence[Mapping[str, Any]]) -> str:
    """The lines the planner and the answer read first: what each name stands for and where that is written, and the names nothing placed."""
    lines = term_lines(terms)
    return ("NAMES IN THIS REQUEST (read before anything else; use these meanings, never another):\n" + "\n".join(lines)) if lines else ""


def open_names(terms: Sequence[Mapping[str, Any]]) -> List[str]:
    return ungrounded(terms)
