"""DEV ONLY: clear open decision-requests this harness itself raised, without running the parked
change, writing a decline that would suppress future proposals for a week, or tripping the
capability-grant learning fold that reads any non-approval resolution as a rejection.

A chat turn that proposes a change (a quest-field edit, a quest-command confirm) parks it on an
"Asks for you" decision-request instead of applying it. Left open across eval runs these (a)
clutter the dev account's ask list and (b) leak into later cases, since an open card rides
quest-backend's live_context into the next turn on the same quest and an open field-edit proposal
makes its own de-duplication treat a later, identical suggestion as already asked.

Going through the normal human resolve path (``resolve_decision_request``) is the wrong tool here.
Approving it would actually run the parked write, a real side effect this cleanup must never
cause. And in quest-backend, EVERY other resolution is read as a rejection by
``finalize_decision_resolution``, two different ways: ``reject``/``decline`` specifically feed
``declined_proposal_fields_for_quest``'s 7-day per-field suppression (so a later, legitimate case
asking for the same field on the same quest would silently get no proposal at all), and ANY
non-approval resolution, whatever the text, feeds ``quest_ai_grants.record_decision_resolution``'s
capability-grant downgrade. There is no resolution string that resolves a row through that path
without one of those two effects.

So this script never calls ``resolve_decision_request``. It flips the row's status straight in
Mongo (the collection write a resolve makes to its OWN bookkeeping fields, nothing else) with none
of the side-effect chain above. The row stays in the database for later debugging; it only stops
being OPEN, so it drops out of every open-decisions listing and out of live_context.

Run by ``runner.py`` (per case, and by the ``cleanup-asks`` subcommand via ``world.cancel_decisions``),
with the backend's own interpreter and the backend directory as the working directory. Refuses
unless:

* the backend's ``ENVIRONMENT`` is development/dev and its Mongo URL points at this machine;
* each decision is OPEN, is SELF-authored (``assigned_to_user_id == created_by``: the eval account
  proposed it to itself, true of everything this harness's chat turns raise), and is not a
  ``machine_quest_creation`` ask (those are ``world.py``'s own setup/teardown lifecycle, tracked in
  ``world_asks.json``; this script never touches one, whatever the caller passes).

Prints one JSON line per decision: {decision_id, ok, reason?}.
"""
import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

DEV_ENVIRONMENTS = {"development", "dev"}
LOCAL_HOSTS = ("localhost", "127.0.0.1")
NEVER_TOUCH_EXECUTABLE_KINDS = {"machine_quest_creation"}


def refuse(reason):
    print(json.dumps({"refused": reason}))
    raise SystemExit(4)


def check_dev_backend():
    backend = Path.cwd()
    if not (backend / "app").is_dir():
        refuse("run from a quest-backend checkout (cwd has no app/)")
    sys.path.insert(0, str(backend))
    from dotenv import load_dotenv
    load_dotenv(backend / ".env", override=True)
    env = (os.environ.get("ENVIRONMENT") or "").strip().lower()
    if env not in DEV_ENVIRONMENTS:
        refuse(f"backend ENVIRONMENT is {env or 'unset'!r}, not development")
    mongo = os.environ.get("MONGODB_URL") or os.environ.get("MONGODB_URI") or ""
    host = mongo.split("@")[-1].split("/")[0]
    if not host.startswith(LOCAL_HOSTS):
        refuse("backend Mongo is not on this machine; refusing to touch anything there")


def cancellable(row):
    """Why ``row`` may not be cancelled, or None when it is safe to clear. Pure (no I/O), so it is
    unit-testable on its own from ``evaluation/qualitative/``."""
    if row is None:
        return "not found"
    if row.get("status") != "open":
        return f"status is {row.get('status')}, not open"
    kind = (row.get("executable") or {}).get("kind")
    if kind in NEVER_TOUCH_EXECUTABLE_KINDS:
        return f"executable kind {kind!r} is never cancelled here"
    assignee, author = row.get("assigned_to_user_id"), row.get("created_by")
    if not assignee or assignee != author:
        return "not self-authored by the account it is assigned to"
    return None


async def cancel(decision_ids):
    from app.core.main_loop import set_main_event_loop
    from app.storage.collections import Collections, get_collection
    set_main_event_loop(asyncio.get_running_loop())
    coll = get_collection(Collections.TEAM_DECISION_REQUESTS)
    now = datetime.datetime.now(datetime.timezone.utc)
    for decision_id in decision_ids:
        row = await coll.find_one({"decision_id": decision_id})
        reason = cancellable(row)
        if reason:
            print(json.dumps({"decision_id": decision_id, "ok": False, "reason": reason}))
            continue
        res = await coll.update_one(
            {"decision_id": decision_id, "status": "open"},
            {"$set": {"status": "resolved", "resolution": "eval_cleanup",
                      "resolved_at": now, "resolved_by": row["assigned_to_user_id"],
                      "auto_resolved": True}})
        print(json.dumps({"decision_id": decision_id, "ok": res.modified_count > 0}))


def main():
    ids = [a for a in sys.argv[1:] if a.startswith("teamdec_")]
    if not ids:
        refuse("no decision ids given")
    check_dev_backend()
    asyncio.run(cancel(ids))


if __name__ == "__main__":
    main()
