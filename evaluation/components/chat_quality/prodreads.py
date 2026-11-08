"""Read evidence for the ``prod_quest_chat`` arm.

The production chat stream reports what a turn read only as a summary (a count, source names and ``operation(<name>)`` locators), never the data. This module
re-performs those reads AFTER the turn, read-only (GET routes only), as the same account on the quest the conversation was pinned to, so the judge and the
unsupported-figure check have the data to compare a reply with. Everything here except ``reread`` is a pure function and offline-tested (``test_prodreads.py``).

A re-read shows the quest as it is after the turn, not as it was when the turn read it. It is labelled that way, and a quest that changed in between can make a
figure look unsupported; that is the price of the gap the stream leaves, and the reason an unreadable locator is recorded rather than guessed.

Not every read can be repeated: a locator with no GET route here, a read frame that names no locator (a collection filter, a search, a file), or a route that
fails leaves the turn with UNVERIFIABLE reads. A figure from such a turn that the re-reads do not show is "unverifiable", not "unsupported".
"""
import json
import re

AFTER_TURN = "re-read after the turn"
UNVERIFIABLE_SOURCE = "unverifiable read"

#: The operation a locator names -> the GET route (and query) that returns the same data for the pinned quest. ``{quest}`` and ``{team}`` are filled from the client.
READ_ROUTES = {
    "get_quest_details": ("/api/quests/{quest}/state", None),
    "get_quest_notes": ("/api/quests/{quest}/notes", None),
    "list_quest_goals": ("/api/teams/{team}/quests/{quest}/goals", None),
    "get_quest_goals": ("/api/teams/{team}/quests/{quest}/goals", None),
    "list_quest_workflows": ("/api/quests/{quest}/workflows", None),
    "list_asks": ("/api/teams/decisions/for-quest", {"quest_id": "{quest}"}),
    "get_task_results": ("/api/assistant-tasks", {"quest_id": "{quest}", "limit": 50}),
    "get_spec": ("/api/intents/specs/quest/{quest}", None),
    "get_quest_spec": ("/api/intents/specs/quest/{quest}", None),
    "list_spec_records": ("/api/intents/specs/quest/{quest}", None),
}

LOCATOR = re.compile(r"operation\(([\w.]+)\)")


def locators_of(events):
    """The distinct locators the stream's ``read`` frames reported, in order."""
    out = []
    for event in events:
        if (event.get("event") or event.get("type")) != "read":
            continue
        for locator in (event.get("data") or {}).get("locators") or []:
            if locator not in out:
                out.append(str(locator))
    return out


def reads_without_locator(events):
    """How many reads the stream counted that named no locator (a collection filter, a search, a file): they cannot be repeated."""
    counted = named = 0
    for event in events:
        if (event.get("event") or event.get("type")) == "read":
            data = event.get("data") or {}
            counted += int(data.get("reads") or 0)
            named += len(data.get("locators") or [])
    return max(0, counted - named)


def plan_rereads(locators, quest_id, team_id):
    """``(calls, unmapped)``: ``calls`` is ``[(locator, path, params)]`` for each locator with a GET route (a route used by two locators is read once), ``unmapped`` the
    locators with none."""
    calls, unmapped, seen = [], [], set()
    for locator in locators:
        match = LOCATOR.fullmatch(locator.strip())
        route = READ_ROUTES.get(match.group(1)) if match else None
        if route is None:
            unmapped.append(locator)
            continue
        path = route[0].format(quest=quest_id, team=team_id)
        params = {k: (v.format(quest=quest_id) if isinstance(v, str) else v) for k, v in (route[1] or {}).items()} or None
        key = (path, json.dumps(params, sort_keys=True))
        if key not in seen:
            seen.add(key)
            calls.append((locator, path, params))
    return calls, unmapped


def reread(client, events, per_item=6000):
    """Repeat the turn's reads read-only. Returns ``(items, unverifiable)``: ``items`` is ``[{"source", "text"}]`` evidence labelled as re-read after the turn,
    ``unverifiable`` the explicit list of reads that could not be repeated (an unmapped locator, a route that failed, reads that named no locator)."""
    calls, unmapped = plan_rereads(locators_of(events), client.quest_id, client.team_id)
    items, unverifiable = [], [f"{locator}: no GET route is known for it" for locator in unmapped]
    for locator, path, params in calls:
        try:
            body = client.send_request("GET", path, params=params)
        except Exception as e:  # noqa: BLE001 -- the read could not be repeated; say so, never fall back to a guess
            unverifiable.append(f"{locator}: GET {path} failed ({type(e).__name__})")
            continue
        items.append({"source": f"{AFTER_TURN}: {locator}", "text": json.dumps(body, default=str)[:per_item]})
    missing = reads_without_locator(events)
    if missing:
        unverifiable.append(f"{missing} read(s) named no operation (a collection filter, a search or a file), so they were not repeated")
    return items, unverifiable


def unverifiable_note(unverifiable):
    return "Reads that could not be repeated after the turn (a figure from them is unverifiable, not contradicted): " + "; ".join(unverifiable) + "." if unverifiable else ""


def classify_figures(figures, unverifiable):
    """``(unsupported, unverifiable_figures)``. A figure the evidence does not show is UNSUPPORTED when every read of the turn was repeated, and UNVERIFIABLE when any
    read could not be (the figure may have come from it). Only the first list counts against a reply."""
    return ([], list(figures)) if unverifiable else (list(figures), [])
