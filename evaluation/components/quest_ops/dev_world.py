"""The disposable dev-Quest fixture for the dev arm: a realistic quest built through the app's own REST
routes, the REST calls the verifiers read, and a cleanup that proves it is gone.

Nothing here runs offline. ``client()`` raises ``DevUnavailable`` unless ``QAR_EVAL_DEV_ENV_FILE`` names the
dev lane's env file (never hardcode it) and the base URL is on the host named by ``QAR_EVAL_DEV_HOST``.
The REST/SSE plumbing is ``evaluation/qualitative/devclient.py``, imported lazily, not copied.

Quest creation is gated (an API key cannot approve its own hold): the fixture falls back, in order, to
``QAR_EVAL_QUEST_ID`` (reuse one approved quest, one run only) and ``QAR_EVAL_QUEST_FACTORY`` (a shell
command whose last stdout line is a new quest id, run in ``QAR_EVAL_QUEST_FACTORY_CWD``). Teardown also runs
``QAR_EVAL_QUEST_DELETE`` (a template with ``{quest_id}``) when set. ``QAR_EVAL_CATEGORY_ID`` picks the
fixture's category, else the first category whose name contains "fitness"."""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

TAG = "ZZEVAL"
QUALITATIVE = Path(__file__).resolve().parents[2] / "qualitative"
HABITS = (("habit", "Morning Run", "binary", None), ("evening_walk", "Evening Walk", "binary", None),
          ("timer", "Stretching Timer", "timer", 15), ("meditation", "Meditation", "timer", 10))
COLLECTION_KEYS = ("habit", "evening_walk", "timer", "meditation", "journal")


class DevUnavailable(RuntimeError):
    """The dev Quest is not configured here: the dev arm is skipped, not failed."""


def configured():
    return bool(os.environ.get("QAR_EVAL_DEV_ENV_FILE"))


def client():
    """The shared dev client, loaded on first use with the env file this arm was pointed at."""
    if not configured():
        raise DevUnavailable("set QAR_EVAL_DEV_ENV_FILE (the dev lane's env file) to run the dev arm")
    os.environ.setdefault("QUAL_DEV_ENV_FILE", os.environ["QAR_EVAL_DEV_ENV_FILE"])
    if str(QUALITATIVE) not in sys.path:
        sys.path.insert(0, str(QUALITATIVE))
    import devclient
    host = (os.environ.get("QAR_EVAL_DEV_HOST") or "").strip().lower()
    if not host or host not in devclient.QUEST_BASE.lower():
        raise DevUnavailable("QAR_EVAL_DEV_HOST must name the dev host that QUEST_BASE_URL points at")
    return devclient


def today():
    return datetime.date.today().isoformat()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unwrap(body):
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        return body["data"]
    return body


def collections_all():
    dc = client()
    status, body = dc.api("GET", "/api/data/collections", params={"limit": 500})
    body = unwrap(body) if status == 200 else []
    return body if isinstance(body, list) else (body.get("collections") or body.get("items") or [])


def system_collection(system_type):
    for coll in collections_all():
        if system_type in (coll.get("system_type"), coll.get("systemType")):
            return coll
    return None


def reflection_days():
    """Local and UTC dates: the chat writes under the account's own zone, which differs for part of a day."""
    utc = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return sorted({today(), utc})


def reflection_entries_by_day(coll_id):
    out = {}
    for entry in client().entries_of(coll_id):
        values = entry.get("fieldValues") or entry.get("field_values") or {}
        day = str(values.get("entry_date") or entry.get("createdAt") or entry.get("created_at") or "")[:10]
        if day in reflection_days():
            out[day] = (entry.get("id"), dict(values))
    return out


def today_all():
    status, body = client().api("GET", "/api/planning/today/all")
    return body if status == 200 and isinstance(body, dict) else {}


def habit_today_row(quest_id, habit_id):
    """The habit row Today's Actions shows for this quest, or None (also checks the standalone group)."""
    for wanted in (quest_id, "__standalone__"):
        for group in today_all().get("quests") or []:
            if group.get("questId") != wanted:
                continue
            for row in group.get("habits") or []:
                if habit_id in (row.get("collectionId"), row.get("collection_id"), row.get("id")):
                    return row
    return None


def timer_state(habit_id):
    status, body = client().api("GET", f"/api/planning/habits/{habit_id}/timer")
    return body if status == 200 and isinstance(body, dict) else {}


def decisions_for_quest(quest_id):
    status, body = client().api("GET", "/api/teams/decisions/for-quest", params={"quest_id": quest_id})
    return body if status == 200 and isinstance(body, list) else []


def open_field_decisions(quest_id, marker=None):
    """Open decisions parking a field edit or reviewed chat code, optionally mentioning ``marker``."""
    out = []
    for row in decisions_for_quest(quest_id):
        if row.get("status") != "open":
            continue
        kinds = {row.get("kind"), (row.get("executable") or {}).get("kind")}
        if not kinds & {"field_edit", "quest_command"}:
            continue
        if marker is not None and marker.lower() not in json.dumps(row.get("executable") or {}).lower():
            continue
        out.append(row)
    return out


def resolve_decision(decision_id, resolution="approve", text=None):
    body = {"resolution": resolution, **({"response_text": text} if text else {})}
    return client().api("POST", f"/api/teams/decisions/{decision_id}/resolve", body)


def cancel_task(task_id):
    """POST .../undo is the real cancel route; 404 and 409 mean nothing to cancel."""
    return client().api("POST", f"/api/assistant-tasks/{task_id}/undo")


def task_status(task_id):
    status, body = client().api("GET", f"/api/assistant-tasks/{task_id}")
    return (body or {}).get("status") if status == 200 and isinstance(body, dict) else None


def run_shell(command, label):
    done = subprocess.run(command, shell=True, cwd=os.environ.get("QAR_EVAL_QUEST_FACTORY_CWD") or None,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {done.returncode}): {done.stderr[-400:]!r}")
    return (done.stdout or "").strip()


def category_id():
    dc = client()
    if os.environ.get("QAR_EVAL_CATEGORY_ID"):
        return os.environ["QAR_EVAL_CATEGORY_ID"]
    status, body = dc.api("GET", "/api/categories/all", params={"limit": 500})
    for cat in (body.get("categories") or []) if isinstance(body, dict) else []:
        if "fitness" in str(cat.get("name") or "").lower():
            return cat.get("id") or cat.get("category_id")
    raise DevUnavailable(f"no fitness category found (status {status}); set QAR_EVAL_CATEGORY_ID")


def create_quest(outcome, criteria, state):
    """POST /api/quests/start; on the creation hold fall back to the operator's id or factory."""
    dc = client()
    status, body = dc.api("POST", "/api/quests/start", {
        "category_id": category_id(), "outcome": outcome, "acceptance_criteria": criteria,
        "current_state": state, "timeline_days": 30, "creation_mode": "quick"})
    if status == 201:
        return body["quest_id"]
    if status not in (202, 409):
        raise RuntimeError(f"quest start failed: {status} {str(body)[:200]}")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    if status == 202 and (body or {}).get("decision_id"):
        resolve_decision(body["decision_id"], "decline", "evaluation harness: using the operator factory")
    quest_id = os.environ.get("QAR_EVAL_QUEST_ID")
    if not quest_id:
        command = os.environ.get("QAR_EVAL_QUEST_FACTORY")
        if not command:
            raise DevUnavailable("quest creation is held; set QAR_EVAL_QUEST_ID or QAR_EVAL_QUEST_FACTORY")
        out = run_shell(command, "QAR_EVAL_QUEST_FACTORY")
        quest_id = out.splitlines()[-1].strip() if out else ""
        if not quest_id:
            raise RuntimeError("QAR_EVAL_QUEST_FACTORY printed no quest id")
    for field, value in (("outcome", outcome), ("acceptance_criteria", criteria), ("current_state", state)):
        code, reply = dc.api("PATCH", f"/api/quests/{quest_id}/field", {"fieldName": field, "value": value})
        if code != 200:
            raise RuntimeError(f"set {field}: {code} {str(reply)[:200]}")
    return quest_id


def collection(dc, quest, name, extra):
    status, body = dc.api("POST", "/api/data/collections",
                          {"name": f"{TAG} {name}", "linked_quest_ids": [quest], **extra})
    if status != 201:
        raise RuntimeError(f"create {name}: {status} {str(body)[:200]}")
    return body["id"]


def setup():
    """Build the fixture and return it: quest, goals A/B/C, four habits, the Run Log, bookkeeping lists."""
    dc = client()
    fx = {"quest": create_quest(
        f"{TAG}-OUTCOME: Run a sub-50-minute 10K by the end of the test window",
        f"{TAG}-AC: a timed 10K under 50:00 recorded on a watch",
        f"{TAG}-STATE: currently running 10K in 58 minutes, three times a week")}
    quest = fx["quest"]
    # Quest AI reads goals through the team-scoped route, so the quest must be on a team.
    dc.api("POST", f"/api/teams/{dc.QUEST_TEAM}/quest", {"quest_id": quest})
    dc.api("PUT", f"/api/quests/{quest}/measurable-outcomes", {"outcomes": [
        {"text": f"{TAG}-MO-1: complete eight consecutive weeks of three runs per week", "completed": False},
        {"text": f"{TAG}-MO-2: record a 10K time under 52:00 as a checkpoint", "completed": False}]})
    fx["goals"] = {}
    for label, name, period, scope in (("A", "Build weekly mileage to 40km", "2026_W41", "week"),
                                       ("B", "Run one tempo session per week", "2026_W41", "week"),
                                       ("C", "Do the October long-run block", "2026_10", "month")):
        status, body = dc.api("POST", "/api/planning/goals", {
            "quest_id": quest, "name": f"{TAG}-GOAL-{label}: {name}", "period": period, "time_scope": scope,
            "criteria": f"{TAG}-CRIT: measured on the running watch"})
        if status != 200:
            raise RuntimeError(f"goal {label}: {status} {str(body)[:200]}")
        fx["goals"][label] = body["id"]
    for key, name, kind, minutes in HABITS:
        freq = {"type": "daily", **({"durationGoalMinutes": minutes} if minutes else {})}
        extra = {"description": f"{TAG} test habit", "type": "habit", "habit_type": kind,
                 "frequency": freq, "custom_fields": []}
        fx[key] = collection(dc, quest, name, extra)
    fx["journal"] = collection(dc, quest, "Run Log", {
        "description": f"{TAG} non-habit journal collection", "type": "journal", "custom_fields": [
            {"id": "distance_km", "name": "Distance km", "type": "number", "required": True},
            {"id": "effort", "name": "Effort", "type": "rating", "rating_min": 1, "rating_max": 5},
            {"id": "notes", "name": "Notes", "type": "multiline"}]})
    day = datetime.date.today()
    for back in (1, 2, 3):  # completionType is the only completion key the entry manager reads
        stamp = (day - datetime.timedelta(days=back)).isoformat()
        dc.api("POST", "/api/data/entries", {
            "collection_id": fx["habit"], "field_values": {"completionType": "yes", "entry_date": stamp},
            "created_at": f"{stamp}T07:30:00Z", "linked_quest_ids": [quest]})
    for back, (km, effort) in zip((1, 2, 4), ((8.2, 4), (5.0, 2), (12.5, 5))):
        stamp = (day - datetime.timedelta(days=back)).isoformat()
        dc.api("POST", "/api/data/entries", {
            "collection_id": fx["journal"], "field_values": {"distance_km": km, "effort": effort,
                                                             "notes": f"{TAG} run on {stamp}"},
            "created_at": f"{stamp}T08:00:00Z", "linked_quest_ids": [quest]})
    # The daily reflection is an account singleton: snapshot today's so teardown can restore it.
    snap, coll = {}, system_collection("daily_reflection")
    if coll:
        snap = {d: {"entry_id": eid, "field_values": fv}
                for d, (eid, fv) in reflection_entries_by_day(coll["id"]).items()}
    fx.update(reflection_snapshot=snap, task_ids=[], decision_ids=[], conversation_ids=[])
    return fx


def resolve_refs(value, fx):
    """Fill {quest}, {habit}, {goal_A}, {today}, {now}, {tag} placeholders in a control step."""
    names = {"today": today(), "now": now_iso(), "tag": TAG, "quest": fx["quest"]}
    names.update({k: fx[k] for k in COLLECTION_KEYS})
    names.update({f"goal_{k}": v for k, v in fx["goals"].items()})
    if isinstance(value, str):
        for key, val in names.items():
            value = value.replace("{" + key + "}", str(val))
        return value
    if isinstance(value, list):
        return [resolve_refs(v, fx) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, fx) for k, v in value.items()}
    return value


def apply_steps(fx, steps):
    """Run a case's control or before steps straight through REST (a positive control, or a precondition)."""
    out = None
    for step in steps or []:
        out = client().api(step["method"], resolve_refs(step["path"], fx), resolve_refs(step.get("body"), fx))
        if out[0] >= 400 or out[0] == 0:
            raise RuntimeError(f"step {step['method']} {step['path']} failed: {out}")
    return out


def teardown(fx):
    """Cancel tasks, decline decisions, delete conversations, restore the reflection, delete goals,
    collections and the quest, then re-fetch to prove it is gone. Returns True when verified."""
    dc, quest = client(), fx["quest"]
    for task in fx.get("task_ids") or []:
        if task_status(task) in ("queued", "in_progress"):
            cancel_task(task)
    for decision in fx.get("decision_ids") or []:
        status, body = dc.api("GET", f"/api/teams/decisions/{decision}")
        if status == 200 and isinstance(body, dict) and body.get("status") == "open":
            resolve_decision(decision, "decline", "evaluation harness cleanup")
    for conv in fx.get("conversation_ids") or []:
        dc.api("DELETE", f"/api/quest-ai/conversations/{conv}")
    coll = system_collection("daily_reflection")
    if coll:
        before = fx.get("reflection_snapshot") or {}
        for day, (entry_id, _values) in reflection_entries_by_day(coll["id"]).items():
            if day in before:
                dc.api("PUT", f"/api/data/entries/{entry_id}", {"field_values": before[day]["field_values"]},
                       params={"collection_id": coll["id"]})
            else:
                dc.api("DELETE", f"/api/data/entries/{entry_id}", params={"collection_id": coll["id"]})
    for goal in dc.goals_of(quest):
        dc.api("DELETE", f"/api/planning/goals/{goal.get('id') or goal.get('goal_id')}")
    for key in COLLECTION_KEYS:
        if fx.get(key):
            dc.api("DELETE", f"/api/data/collections/{fx[key]}")
    dc.api("DELETE", f"/api/quests/{quest}")
    template = os.environ.get("QAR_EVAL_QUEST_DELETE")
    if template:
        try:
            run_shell(template.format(quest_id=quest), "QAR_EVAL_QUEST_DELETE")
        except Exception as e:  # noqa: BLE001
            print(f"QAR_EVAL_QUEST_DELETE failed for {quest}: {e}")
    status, body = dc.api("GET", "/api/quests/me")
    gone = status == 200 and isinstance(body, list) and quest not in [q.get("quest_id") for q in body]
    gone &= not dc.goals_of(quest)
    gone &= not [c for c in collections_all() if TAG in str(c.get("name", ""))]
    gone &= not [t for t in fx.get("task_ids") or [] if task_status(t) in ("queued", "in_progress")]
    return bool(gone)
