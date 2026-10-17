"""DEV ONLY: approve the eval world's OWN quest-creation asks through quest-backend's resolve path.

Quest parks every quest an API key asks to create on an "Asks for you" decision, and an API key
can never approve it. On a DEV backend the asks this harness files for its own disposable world are
test fixtures, so the harness resolves them itself instead of asking a person to sign in. It does
that in-process, inside a quest-backend checkout, by calling the same
``resolve_decision_request(decision_id, assignee, "approve")`` every human approve path calls, so
the held quest is created with all of its side effects (quest card, index sync, analytics).

Run by ``runner.py setup --approve-own-asks`` (which passes the decision ids), with the backend's
own interpreter and the backend directory as the working directory. Refuses unless:

* the backend's ``ENVIRONMENT`` is development/dev and its Mongo URL points at this machine;
* each decision is OPEN, is a ``machine_quest_creation``, carries the eval tag in its payload, and
  was requested by the same account it is assigned to (the harness's own account asked for it).

It never declines anything. Prints one JSON line per decision: {decision_id, ok, quest, reason}.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

TAG = "ZZQEVAL"
DEV_ENVIRONMENTS = {"development", "dev"}
LOCAL_HOSTS = ("localhost", "127.0.0.1")


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
        refuse("backend Mongo is not on this machine; refusing to approve anything there")


def own_eval_ask(row):
    """Why this row may not be approved, or None when it is the harness's own open quest ask."""
    if row is None:
        return "not found"
    if row.get("status") != "open":
        return f"status is {row.get('status')}"
    executable = row.get("executable") or {}
    if executable.get("kind") != "machine_quest_creation":
        return "not a quest-creation ask"
    if TAG not in json.dumps(executable.get("payload") or {}, default=str):
        return "payload carries no eval tag"
    assignee = row.get("assigned_to_user_id")
    if not assignee or assignee != executable.get("caller_id") or assignee != row.get("created_by"):
        return "not requested by the account it is assigned to"
    return None


async def approve(decision_ids):
    from app.core.main_loop import set_main_event_loop
    from app.storage.collections import Collections, get_collection
    from app.business.teams import team_operations as teams
    set_main_event_loop(asyncio.get_running_loop())
    coll = get_collection(Collections.TEAM_DECISION_REQUESTS)
    for decision_id in decision_ids:
        row = await coll.find_one({"decision_id": decision_id})
        reason = own_eval_ask(row)
        if reason:
            print(json.dumps({"decision_id": decision_id, "ok": False, "reason": reason}))
            continue
        ok = await teams.resolve_decision_request(decision_id, row["assigned_to_user_id"], "approve")
        after = await coll.find_one({"decision_id": decision_id}) or {}
        print(json.dumps({"decision_id": decision_id, "ok": bool(ok),
                          "result": str(after.get("execution_result") or "")[:200]}))


def main():
    ids = [a for a in sys.argv[1:] if a.startswith("teamdec_")]
    if not ids:
        refuse("no decision ids given")
    check_dev_backend()
    asyncio.run(approve(ids))


if __name__ == "__main__":
    main()
