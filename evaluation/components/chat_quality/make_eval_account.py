"""Create (idempotently) or wipe the dedicated, hermetic chat_quality eval identity on the DEV database.

Run with the backend interpreter and the backend directory as cwd (the same shape as
quest-backend/scripts/checks/workflow_chat_cert/mkkeys.py):

    cd <quest-backend> && venv/bin/python3 <this file> [--wipe] [--env-file PATH]

Create (default): ensures one stand-in user on a reserved example domain, a private team it owns, its default
rep (empty: no saved corrections), a subscription so AI features work, and its OWN api key. Writes a new env file
(QAR_EVAL_ACCOUNT_ENV_FILE, default: a gitignored file next to the shared dev lane env file) holding
QUEST_BASE_URL (copied from the shared env file), QUEST_API_KEY and QUEST_TEAM_ID, mode 0600. The key is never
printed; only the file path is. Re-running keeps a working key, or mints a new one if the file or key is gone.

--wipe: deletes everything owned by this identity (conversations, tasks, decision-requests, quests, goals,
collections, notes, cards ...) and resets the rep to empty, so the account is back to a known empty state.
It refuses unless the user it is about to wipe is exactly the eval identity (email AND marker flag AND the
team's owner), and it never touches another user's rows.

DEV ONLY: the backend .env must name the database quest_db and the base URL must be the dev host.
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from bson import ObjectId
from dotenv import dotenv_values
from pymongo import MongoClient

EMAIL = "qa-eval-chat@example.test"
CLERK_ID = "user_standin_qa_eval_chat"
NAME = "QA Eval Chat"
TEAM_ID = "team_qaevalchat01"
TEAM_NAME = "QA Eval Chat Team (dev only)"
MEMBERSHIP_ID = "teammbr_qaevalchat01"
REP_ID = "rep_qaevalchat01"
KEY_NAME = "chat-quality-eval-hermetic"
MARKER = "is_chat_quality_eval_identity"

BACKEND_ENV = Path.cwd() / ".env"
PRODUCT = Path(__file__).resolve().parents[4].parent  # .../product (this repo sits in product/launch_code)
SHARED_ENV = Path(os.environ.get("QUAL_SHARED_ENV_FILE") or PRODUCT / "setup" / "sd-dev-runner" / ".env")
DEFAULT_OUT = PRODUCT / "setup" / "sd-dev-runner" / "eval-account.env"

# Collections the wipe never deletes from: the identity itself (kept so the account can be reused).
KEEP = {"users", "api_keys", "teams", "team_memberships", "subscriptions"}
OWNER_FIELDS = ("user_id", "owner_user_id", "created_by", "owner_id", "requested_by", "assigned_to",
                "updated_by", "completed_by", "owner_user_ids")


def connect():
    env = dotenv_values(BACKEND_ENV)
    uri = env.get("MONGODB_URI") or env.get("MONGODB_URL")
    if not uri or env.get("MONGODB_DATABASE") != "quest_db":
        raise SystemExit("make_eval_account writes to the dev database (quest_db) only")
    if (env.get("ENVIRONMENT") or "").strip().lower() not in ("development", "dev"):
        raise SystemExit("backend ENVIRONMENT is not development; refusing")
    return MongoClient(uri, authSource=env.get("MONGODB_AUTH_SOURCE") or None)[env["MONGODB_DATABASE"]]


def parse_env(path):
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def dev_base_url():
    base = (parse_env(SHARED_ENV).get("QUEST_BASE_URL") or "").rstrip("/")
    if "batmanhq" not in base or "spiritualdata.org" in base:
        raise SystemExit("shared env base URL is not the dev backend; refusing")
    return base


def eval_user(db):
    """The eval user, only if it is exactly the eval identity; None if absent."""
    user = db.users.find_one({"email": EMAIL})
    if user is None:
        return None
    team = db.teams.find_one({"team_id": TEAM_ID})
    if not (user.get(MARKER) is True and user.get("clerk_user_id") == CLERK_ID
            and team and team.get("created_by") == str(user["_id"])):
        raise SystemExit("a user with the eval email exists but is not the eval identity; refusing")
    return user


def ensure_identity(db):
    now = datetime.utcnow().replace(microsecond=0)
    ends = now + timedelta(days=365 * 3)
    user = db.users.find_one({"email": EMAIL})
    if user is None:
        uid = ObjectId()
        db.users.insert_one({
            "_id": uid, "user_id": str(uid), "email": EMAIL, "clerk_user_id": CLERK_ID, "username": "qa-eval-chat",
            "name": NAME, "first_name": "QA", "last_name": "Eval", "display_name": NAME, "role": "user",
            "created_at": now, "updated_at": now, "wisdom_points": 0, "onboarding_completed": True,
            "note": "dev-only identity for the chat_quality hermetic evaluation; wiped by make_eval_account.py --wipe",
            "is_test_standin": True, MARKER: True,
            "preferences": {"enabled_features": ["quest", "data"], "timezone": "America/Los_Angeles",
                            "detected_timezone": "America/Los_Angeles", "assistant_tasks_enabled": True,
                            "assistant_tasks_opt_out": False, "ai_rep_goal_handling": "auto", "preferred_language": "en",
                            "onboarding_completed": True, "searchable_by_mentors": False},
            "subscription": {"user_id": uid, "tier": "full", "is_active": True, "subscription_started_at": now.isoformat(),
                             "subscription_ends_at": ends.isoformat(), "source": "dev_eval", "created_at": now.isoformat(),
                             "updated_at": now.isoformat()},
            "subscription_status": "active", "subscription_type": "full", "subscription_plan_type": "full",
            "subscription_start": now, "subscription_end": ends, "email_unsubscribed": True, "notification_preferences":
                {"email_enabled": False, "push_enabled": False, "push_token": None},
        })
    user = eval_user(db) if db.teams.find_one({"team_id": TEAM_ID}) else db.users.find_one({"email": EMAIL})
    uid = user["_id"]
    sid = str(uid)
    if not db.teams.find_one({"team_id": TEAM_ID}):
        db.teams.insert_one({"_id": TEAM_ID, "team_id": TEAM_ID, "name": TEAM_NAME, "description": "Dev-only team of the hermetic chat_quality eval identity.",
                             "town_id": None, "org_id": None, "parent_team_id": None, "visibility": "private",
                             "created_by": sid, "created_at": now, "updated_at": now})
    user = eval_user(db)
    if not db.team_memberships.find_one({"team_id": TEAM_ID, "user_id": sid}):
        db.team_memberships.insert_one({"_id": MEMBERSHIP_ID, "membership_id": MEMBERSHIP_ID, "team_id": TEAM_ID,
                                        "user_id": sid, "role": "owner", "joined_at": now})
    if not db.subscriptions.find_one({"user_id": sid}):
        db.subscriptions.insert_one({"user_id": sid, "tier": "full", "is_active": True, "subscription_started_at": now.isoformat(),
                                     "subscription_ends_at": ends.isoformat(), "created_at": now.isoformat(), "updated_at": now.isoformat()})
    reset_rep(db, sid, now)
    return user


def reset_rep(db, sid, now=None):
    now = now or datetime.utcnow()
    db.ai_reps.delete_many({"owner_user_id": sid, "rep_id": {"$ne": REP_ID}})
    db.ai_reps.update_one({"rep_id": REP_ID}, {"$set": {
        "owner_user_id": sid, "team_id": TEAM_ID, "teams": [TEAM_ID], "display_name": "QA Eval", "persona": None, "area": None,
        "is_default": True, "learned_notes": [], "context_prefs": [], "decision_guidance": [], "created_by": sid,
        "updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True)


def key_is_good(db, uid, key):
    if not key:
        return False
    doc = db.api_keys.find_one({"key_hash": hashlib.sha256(key.encode()).hexdigest()})
    return bool(doc and doc.get("is_active") and str(doc["user_id"]) == str(uid)
                and (doc.get("expires_at") is None or doc["expires_at"] > datetime.utcnow() + timedelta(days=7)))


def write_env(path, base, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"# Hermetic chat_quality eval identity (dev only). Never commit. Made by make_eval_account.py.\n"
                     f"QUEST_BASE_URL={base}\nQUEST_API_KEY={key}\nQUEST_TEAM_ID={TEAM_ID}\n")
    os.chmod(path, 0o600)


def create(out):
    db = connect()
    base = dev_base_url()
    user = ensure_identity(db)
    existing = parse_env(out).get("QUEST_API_KEY")
    if key_is_good(db, user["_id"], existing) and parse_env(out).get("QUEST_TEAM_ID") == TEAM_ID:
        print("eval identity ready; existing key kept; env file:", out)
        return
    db.api_keys.update_many({"user_id": user["_id"], "name": KEY_NAME}, {"$set": {"is_active": False}})
    key = "qsk_" + secrets.token_hex(24)
    now = datetime.utcnow()
    db.api_keys.insert_one({"user_id": user["_id"], "name": KEY_NAME, "key_hash": hashlib.sha256(key.encode()).hexdigest(),
                            "key_prefix": key[:12] + "...", "created_at": now, "expires_at": now + timedelta(days=365), "is_active": True})
    write_env(out, base, key)
    print("eval identity ready; new key written; env file:", out)


def delete_cards(base, key):
    """Delete the identity's context cards through the backend API (cards live in the card store, not Mongo)."""
    def call(method, path):
        req = urllib.request.Request(base + path, method=method, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")
    try:
        cards = call("GET", "/api/cards?include_managed=true").get("cards", [])
    except Exception as exc:
        print("card listing failed:", type(exc).__name__)
        return 0
    removed = 0
    for card in cards:
        try:
            call("DELETE", "/api/cards/" + card["id"])
            removed += 1
        except Exception:
            pass
    return removed


def wipe(out):
    db = connect()
    user = eval_user(db)
    if user is None:
        raise SystemExit("eval identity does not exist; run without --wipe first")
    uid, sid = user["_id"], str(user["_id"])
    ids = [uid, sid]
    removed = {}
    env = parse_env(out)
    if env.get("QUEST_API_KEY") and env.get("QUEST_TEAM_ID") == TEAM_ID and key_is_good(db, uid, env["QUEST_API_KEY"]):
        removed["cards(api)"] = delete_cards(dev_base_url(), env["QUEST_API_KEY"])
    for name in db.list_collection_names():
        if name in KEEP or name.startswith("system."):
            continue
        if name == "ai_reps":
            continue  # the rep is reset below, not deleted
        clauses = [{f: {"$in": ids}} for f in OWNER_FIELDS] + [{"team_id": TEAM_ID}, {"teams": TEAM_ID}]
        result = db[name].delete_many({"$or": clauses})
        if result.deleted_count:
            removed[name] = result.deleted_count
    reset_rep(db, sid)
    db.users.update_one({"_id": uid}, {"$set": {"wisdom_points": 0}})
    print("wiped:", json.dumps(removed, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--wipe", action="store_true", help="delete everything the eval identity owns")
    parser.add_argument("--env-file", default=os.environ.get("QAR_EVAL_ACCOUNT_ENV_FILE") or str(DEFAULT_OUT))
    args = parser.parse_args()
    out = Path(args.env_file)
    wipe(out) if args.wipe else create(out)


if __name__ == "__main__":
    main()
