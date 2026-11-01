"""Structural check of a finished turn's work trace: no model, no network, no wording read.

``check_turn(calls, events, ...)`` takes what the turn really did (the structural results of its calls) and what the trace
said (the ``work_trace`` events the run emitted, as ``ProgressEvent`` objects or their ``data`` dicts) and returns the
violations. An esoi metric scores a turn as passing when the list is empty (``passes``), or as the share of checks held
(``score``). Three rules:

1. Honest counts. A trace never claims more than the run did: ``web.read`` is at most the pages the calls fetched,
   ``web.found`` at most the results the searches listed, ``papers.read`` at most the works the literature calls returned,
   ``records.read`` at most the cards the answer USED, ``links.checked`` at most the verdicts the link guard held. A card the
   assembler only selected is not a record read: when the call records say which cards were used (``"used": bool`` on a card), a
   records count or item beyond the used ones is a violation, and an unrelated question that shows any records step is too. The
   same for papers: when the works say which the reply cited (``"cited": bool``), ``papers.read`` is at most the cited ones.
2. No leak. No private title (``private_titles``: the labels of records that must not be shown) appears in any event a
   shared thread's viewer gets, and in a shared thread no event of kind ``records`` or ``action`` carries items. No event holds a
   full URL, a query string, a file path or a newline in a label, and every event passes the hub's validator.
3. Terminal states. Every step reaches ``done`` or ``failed`` by the end of the turn, and the latest event per ``step_id`` is the one
   judged (replace-by-``step_id``).

``calls`` is a list of structural records, one per call, built from results and never from model text:

    {"kind": "web_search", "hits": [{"url": ...}, ...]}      {"kind": "web_page", "url": ..., "ok": bool}
    {"kind": "papers", "works": [{"cited": bool}, ...], "total": n}   {"kind": "records", "cards": [{"id": ..., "used": bool}, ...]}
    {"kind": "links", "verdicts": [...]}
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from hubofhubs.ports.trace import validate

PRIVATE_KINDS = ("records", "action")
URL_OR_PATH = re.compile(r"https?://|www\.|\?[a-z_]+=|(?:^|\s)(?:/|~/)[\w.-]+/")


def as_data(event: Any) -> Dict[str, Any]:
    """The event dict from a ``ProgressEvent`` or the dict itself."""
    data = getattr(event, "data", event)
    return dict(data) if isinstance(data, Mapping) else {}


def latest_by_step(events: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for event in events:
        data = as_data(event)
        if data.get("step_id"):
            out[str(data["step_id"])] = data
    return out


def actual_totals(calls: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """What the calls really did, by counted thing."""
    t = {"web_found": 0, "web_read": 0, "papers_found": 0, "papers_read": 0, "records": 0, "links": 0}
    cards = set()
    for call in calls:
        kind = call.get("kind")
        if kind == "web_search":
            t["web_found"] += len(call.get("hits") or [])
        elif kind == "web_page" and call.get("ok"):
            t["web_read"] += 1
        elif kind == "papers":
            works = call.get("works") or []
            # Works that say whether the reply cited them: only those are read. Without the flag every returned work counts.
            t["papers_read"] += sum(1 for w in works if w.get("cited")) if any(isinstance(w, Mapping) and "cited" in w for w in works) else len(works)
            t["papers_found"] += max(int(call.get("total") or 0), len(works))
        elif kind == "records":
            listed = [c for c in call.get("cards") or [] if isinstance(c, Mapping)]
            flagged = any("used" in c for c in listed)
            cards.update(str(c.get("id")) for c in listed if c.get("used") or not flagged)
        elif kind == "links":
            t["links"] += len(call.get("verdicts") or [])
    t["records"] = len(cards)
    return t


def check_turn(calls: Sequence[Mapping[str, Any]], events: Iterable[Any], *, shared: bool = False,
               private_titles: Sequence[str] = ()) -> List[str]:
    """The violations (plain sentences), empty when the trace is honest, leak-free and finished."""
    events = list(events)
    problems: List[str] = []
    steps = latest_by_step(events)
    totals = actual_totals(calls)

    # 1. Honest counts, judged on each step's latest event.
    limits = {"web": ("found", totals["web_found"], "read", totals["web_read"]),
              "papers": ("found", totals["papers_found"], "read", totals["papers_read"]),
              "records": ("found", totals["records"], "read", totals["records"]),
              "links": ("checked", totals["links"], None, 0)}
    for step_id, (first, first_max, second, second_max) in limits.items():
        counts = (steps.get(step_id) or {}).get("counts") or {}
        if counts.get(first, 0) > first_max:
            problems.append(f"{step_id}: counts.{first}={counts[first]} but the run only did {first_max}")
        if second and counts.get(second, 0) > second_max:
            problems.append(f"{step_id}: counts.{second}={counts[second]} but the run only did {second_max}")
    # A records step that lists a card the answer did not use, or any records step when none was used.
    used_flags = [c for call in calls if call.get("kind") == "records" for c in call.get("cards") or [] if isinstance(c, Mapping)]
    if any("used" in c for c in used_flags):
        used_ids = {str(c.get("id")) for c in used_flags if c.get("used")}
        records = steps.get("records") or {}
        if records and not used_ids:
            problems.append("records: a records step was shown but no record was used by the answer")
        for item in records.get("items") or []:
            if str(item.get("ref")) not in used_ids:
                problems.append(f"records: lists a card the answer did not use ({item.get('ref')})")
    if "links" in steps and (steps["links"].get("counts") or {}).get("failed", 0) > (steps["links"].get("counts") or {}).get("checked", 0):
        problems.append("links: more links failed than were checked")

    # 2. No leak, on every event sent (not only the latest): a viewer saw each of them.
    for data in (as_data(e) for e in events):
        for issue in validate(data):
            problems.append(f"{data.get('step_id')}: {issue}")
        blob = json.dumps(data)
        for title in private_titles:
            if title and title in blob:
                problems.append(f"{data.get('step_id')}: a private title appears in the trace")
        if shared and data.get("kind") in PRIVATE_KINDS and data.get("items"):
            problems.append(f"{data.get('step_id')}: a shared thread's {data.get('kind')} step carries items, counts only is allowed")
        for item in data.get("items") or []:
            for key in ("ref", "label", "sub"):
                if URL_OR_PATH.search(str(item.get(key) or "")):
                    problems.append(f"{data.get('step_id')}: item {key} holds a URL, query or path")

    # 3. Terminal states.
    for step_id, data in steps.items():
        if data.get("state") not in ("done", "failed"):
            problems.append(f"{step_id}: ended the turn still {data.get('state')}")
    return problems


def passes(calls: Sequence[Mapping[str, Any]], events: Iterable[Any], **kwargs: Any) -> bool:
    return not check_turn(calls, events, **kwargs)


def score(cases: Iterable[Mapping[str, Any]]) -> float:
    """The share of cases (each ``{"calls", "events", "shared", "private_titles"}``) whose trace passes every rule."""
    cases = list(cases)
    if not cases:
        return 1.0
    held = sum(1 for c in cases if passes(c.get("calls") or [], c.get("events") or [], shared=bool(c.get("shared")),
                                          private_titles=c.get("private_titles") or ()))
    return held / len(cases)
