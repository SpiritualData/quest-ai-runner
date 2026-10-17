"""The disposable qualitative-eval WORLD on DEV, built only through the app's own REST endpoints.

Five quests in five domains (so "pick the right quest" is itself testable), each with an outcome,
current state, acceptance criteria, measurable outcomes, goals and notes, plus collections of every
useful shape (binary habit, timer habit, journal with custom fields, list/table tracker, expense
log, reading log) linked to DIFFERENT quests, and seeded entries/notes that carry PIVOT FACTS: one
specific fact whose presence should change the correct answer ("knee injury", "budget overrun",
"deadline moved"). Dataset authors reference those facts by name through ``PIVOTS``.

    python3 evaluation/components/chat_quality/world.py setup      # build (idempotent: refuses if a world exists)
    python3 evaluation/components/chat_quality/world.py teardown   # delete everything, re-fetch to prove it
    python3 evaluation/components/chat_quality/world.py show       # print ids and pivots

Everything carries the tag ``ZZQEVAL``: quests in their acceptance criteria and current state,
collections in their description, quest-doc / team-context entries in their name. Teardown works
from the state file AND sweeps the tag, so a half-built world is still cleaned.

AUTOPILOT IS PART OF THE WORLD (2026-10-06). quest-backend refuses every AI write to a quest's own
fields (outcome, current_state, acceptance_criteria, preferences, timeline_days, measurable
outcomes) while that quest's ``autopilot.mode`` is ``off``, which is the default on a freshly
created quest: the write is turned into a decision-request instead, and the chat answers "turn
autopilot on for this quest". So a world left at the default could not test the field-write path at
all, and every field case would have failed for a reason that is correct product behaviour. ``setup``
therefore puts four quests on ``mode="act"`` (the owner has opted in) and deliberately leaves
``family`` on ``off`` so the proposal path is tested too. See ``AUTOPILOT_BY_QUEST`` and
``arm_autopilot``.

DEV ONLY (devclient refuses to load otherwise).
"""
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devclient import (  # noqa: E402
    QUEST_TEAM, TAG, WORK_DIR, api, entries_of, goals_of, list_collections, list_quests,
    measurable_outcomes_of, notes_of, quest_state, unwrap_list)

STATE_PATH = WORK_DIR / "world.json"
FALLBACK_CATEGORY = "cat_df82187e53c0"  # a known dev category ("Fitness"); used when a name lookup misses
AUTOPILOT_PASS_KIND = "autopilot"  # task_kind of the lane's recurring pass (runner/autopilot.py)

from world_spec import (  # noqa: E402,F401  (re-exported: the datasets and tests read these from world)
    AUTOPILOT_BY_QUEST, COLLECTIONS, MONTH_PERIOD, PIVOTS, QUESTS, TEAM_CONTEXT, TODAY, ago, ahead, habit_spec,
    week_period)


def arm_autopilot(quest_keys, quest_ids):
    """Set each quest's autopilot mode per ``AUTOPILOT_BY_QUEST``.

    ``last_pass_at`` is stamped to now and the cadence set to monthly in the same PATCH so the
    deployed autopilot scanner does not decide one of these quests is due mid-run and write side
    effects the eval would read as the chat's own.
    """
    for key in quest_keys:
        mode = AUTOPILOT_BY_QUEST.get(key, "off")
        body = {"mode": mode, "cadence": "monthly",
                "last_pass_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        status, resp = api("PATCH", f"/api/quests/{quest_ids[key]}/autopilot", body)
        if status not in (200, 201):
            print(f"  WARN autopilot {mode} for {key} failed: {status} {str(resp)[:200]}")
        else:
            print(f"autopilot[{key}] = {mode}")



# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------

def category_lookup():
    status, body = api("GET", "/api/categories/all")
    cats = body.get("categories") if status == 200 and isinstance(body, dict) else []
    by_name = {}
    for c in cats:
        by_name.setdefault(c.get("name"), c.get("id"))
    return by_name


def build_collection(key, spec, quest_ids):
    linked = [quest_ids[q] for q in spec["linked"]]
    description = f"{spec['description']} ({TAG})"
    if spec["type"] == "habit":
        body = {"name": spec["name"], "description": description, "type": "habit",
                "habit_type": spec["habit_type"], "custom_fields": [], "linked_quest_ids": linked}
        if spec["habit_type"] == "timer":
            body["frequency"] = {"type": "daily", "durationGoalMinutes": spec["timer_minutes"]}
        else:
            body["frequency"] = {"type": "daily"}
            body["quick_entry_enabled"] = True
    else:
        body = {"name": spec["name"], "description": description, "type": spec["type"],
                "custom_fields": spec["fields"], "linked_quest_ids": linked}
    status, resp = api("POST", "/api/data/collections", body)
    assert status == 201, (key, status, resp)
    cid = resp["id"]
    for days, hour, values in spec["entries"]:
        if spec["type"] == "habit":
            # A habit entry is one row per period and the endpoint stamps it with TODAY unless the
            # period fields are explicit. (A timer habit's seconds cannot be seeded: the backend
            # derives them from session records, so a seeded timer row is completed with 0 s.)
            day = (TODAY - datetime.timedelta(days=days)).isoformat()
            values = {"entry_date": day, "period_start": day, "period_end": day, "period": "day",
                      "completed": True, "completionType": "yes"}
        st, er = api("POST", "/api/data/entries", {
            "collection_id": cid, "field_values": values, "created_at": ago(days, hour),
            "linked_quest_ids": linked})
        if st not in (200, 201):
            print(f"  WARN entry for {key} failed: {st} {str(er)[:200]}")
    return cid


ASKS_PATH = WORK_DIR / "world_asks.json"


def find_quest(spec):
    """An already-created world quest for this spec (matched by outcome + tag), else None."""
    for q in list_quests():
        state = q.get("state") or {}
        if state.get("outcome") == spec["outcome"] and TAG in str(
                state.get("acceptance_criteria") or ""):
            return q.get("quest_id")
    return None


def request_quest(spec, cats):
    """File the quest creation. An API-key caller is an AI actor, so Quest PARKS the request on an
    'Asks for you' decision (HTTP 202) until a PERSON approves it in a signed-in session; an API
    key can never approve it. Returns (quest_id or None, decision_id or None)."""
    category = next((cats[n] for n in spec["category_names"] if n in cats), FALLBACK_CATEGORY)
    status, body = api("POST", "/api/quests/start", {
        "category_id": category, "outcome": spec["outcome"],
        "acceptance_criteria": f"{spec['acceptance_criteria']} ({TAG})",
        "current_state": spec["current_state"],
        "timeline_days": spec["timeline_days"], "creation_mode": "quick"})
    if status == 201:
        return body["quest_id"], None
    if status == 202 and isinstance(body, dict):
        return None, body.get("decision_id")
    raise RuntimeError(f"quest creation failed: {status} {body}")


BACKEND_DIR = Path(os.environ.get("QUAL_BACKEND_DIR")
                   or Path(__file__).resolve().parents[4] / "quest-backend")


def approve_own_asks(decision_ids):
    """DEV ONLY: approve the world's OWN quest-creation asks via quest-backend's human resolve path.

    These asks are test fixtures this harness filed on the dev backend, so no person is asked to
    sign in for them. ``approve_own_asks.py`` runs inside a quest-backend checkout
    (``QUAL_BACKEND_DIR``, default: a sibling ``quest-backend``) with its interpreter
    (``QUAL_BACKEND_PYTHON``, default ``venv/bin/python3`` there) and itself refuses unless that
    backend is development with a local Mongo and each ask is an open, tagged quest-creation ask
    the harness's own account requested. devclient has already refused any non-dev base URL."""
    import subprocess
    if not decision_ids:
        return
    python = os.environ.get("QUAL_BACKEND_PYTHON") or str(BACKEND_DIR / "venv" / "bin" / "python3")
    script = Path(__file__).resolve().parent / "approve_own_asks.py"
    proc = subprocess.run([python, str(script), *decision_ids], cwd=BACKEND_DIR,
                          capture_output=True, text=True, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    for line in lines:
        print(f"  approve-own-asks: {line}")
    if proc.returncode != 0:
        raise SystemExit(f"approve-own-asks failed (exit {proc.returncode}): "
                         f"{(proc.stderr or '').strip().splitlines()[-1:]}")


def ensure_quests(wait_seconds=0, approve_own=False):
    """Make sure every world quest exists. Files the approval asks for missing ones, then polls up
    to ``wait_seconds`` for a person to approve them (or, with ``approve_own`` on dev, approves the
    harness's own asks itself; see ``approve_own_asks``). Returns ({key: quest_id},
    {key: decision_id}) where the second dict is what is still pending."""
    import time
    cats = category_lookup()
    asks = json.loads(ASKS_PATH.read_text()) if ASKS_PATH.exists() else {}
    quests, pending = {}, {}
    deadline = time.time() + wait_seconds
    approved_once = False
    while True:
        quests, pending = {}, {}
        for key, spec in QUESTS.items():
            qid = find_quest(spec)
            if qid:
                quests[key] = qid
                asks.pop(key, None)
                continue
            if key not in asks:
                qid, decision = request_quest(spec, cats)
                if qid:
                    quests[key] = qid
                    continue
                asks[key] = decision
            pending[key] = asks[key]
        ASKS_PATH.write_text(json.dumps(asks, indent=1))
        if pending and approve_own and not approved_once:
            approve_own_asks(list(pending.values()))
            approved_once = True
            deadline = max(deadline, time.time() + 60)  # let the quest listing catch up
            continue
        if not pending or time.time() >= deadline:
            return quests, pending
        time.sleep(10)


def build_contents(state, quest_keys):
    """Goals, measurable outcomes, notes, quest docs, collections and team context."""
    out = state
    for key in quest_keys:
        spec, qid = QUESTS[key], out["quests"][key]
        api("POST", f"/api/teams/{QUEST_TEAM}/quest", {"quest_id": qid})  # lets Quest AI read goals
        api("PUT", f"/api/quests/{qid}/measurable-outcomes", {
            "outcomes": [{"text": t, "completed": False} for t in spec["measurable_outcomes"]]})
        out["goals"][key] = {}
        for name, period, scope, criteria in spec["goals"]:
            st, g = api("POST", "/api/planning/goals", {
                "quest_id": qid, "name": name, "period": period, "time_scope": scope,
                "criteria": criteria})
            assert st == 200, (key, name, st, g)
            out["goals"][key][name] = g["id"]
        for text, pivot in spec["notes"]:
            st, _ = api("POST", f"/api/quests/{qid}/notes", {"text": text})
            if st not in (200, 201):
                print(f"  WARN note failed {st}")
        for title, content, pivot in spec.get("docs", []):
            st, d = api("POST", f"/api/quests/{qid}/context-entries",
                        {"name": f"{TAG} {title}", "content": content})
            if st in (200, 201) and isinstance(d, dict) and d.get("id"):
                out["docs"][title] = {"quest": key, "id": d["id"]}
            else:
                print(f"  WARN quest doc failed {st} {str(d)[:200]}")
        STATE_PATH.write_text(json.dumps(out, indent=1))
        print(f"contents of quest {key} built")
    arm_autopilot([k for k in quest_keys if k in out["quests"]], out["quests"])
    for key, spec in COLLECTIONS.items():
        if all(q in out["quests"] for q in spec["linked"]):
            out["collections"][key] = build_collection(key, spec, out["quests"])
            print(f"collection {key}: {out['collections'][key]}")
            STATE_PATH.write_text(json.dumps(out, indent=1))
    st, d = api("POST", f"/api/teams/{QUEST_TEAM}/context-entries",
                {"name": TEAM_CONTEXT["name"], "content": TEAM_CONTEXT["content"]})
    if st in (200, 201) and isinstance(d, dict) and d.get("id"):
        out["team_context"] = d["id"]
    else:
        print(f"  WARN team context failed {st} {str(d)[:200]}")
    out["contents_built"] = True
    # Every seeded entry date is relative to the day the contents were built, so the ground truth
    # must list them from THAT day, not from the run's today (see ``ground_truth``).
    out["contents_seeded_on"] = TODAY.isoformat()
    STATE_PATH.write_text(json.dumps(out, indent=1))


def fresh_state():
    return {"quests": {}, "goals": {}, "collections": {}, "docs": {}, "team_context": None,
            "contents_built": False, "pivots": PIVOTS, "built": datetime.datetime.now().isoformat()}


def setup(wait_seconds=0, approve_own=False):
    """Build the world. Needs one-time approval of the five quest creations (see request_quest):
    by a person, or on dev by the harness itself with ``approve_own``. Exits 3 if any are still
    pending; re-run `setup` after approving."""
    if STATE_PATH.exists() and json.loads(STATE_PATH.read_text()).get("contents_built"):
        raise SystemExit(f"{STATE_PATH} exists: a world is already built. Run teardown or reset.")
    quests, pending = ensure_quests(wait_seconds, approve_own)
    if pending:
        print("\nQUEST APPROVALS PENDING. A person must approve these on dev (the app's "
              "'Asks for you', signed in; an API key cannot approve):")
        for key, decision in pending.items():
            print(f"  {key}: {decision}   outcome: {QUESTS[key]['outcome']}")
        print(f"\nThen re-run setup. Asks are remembered in {ASKS_PATH}.")
        raise SystemExit(3)
    state = fresh_state()
    state["quests"] = quests
    state["cards_baseline"] = sorted(card_ids())
    STATE_PATH.write_text(json.dumps(state, indent=1))
    build_contents(state, list(QUESTS))
    print(f"world written to {STATE_PATH}")
    return state


def setup_partial():
    """SMOKE MODE ONLY: no quests (no approvals needed). Builds the collections that need no quest,
    unlinked, plus team context, so the runner/judge pipeline can be exercised end to end. A
    partial world cannot serve the datasets; it is labelled so in the state file."""
    if STATE_PATH.exists():
        raise SystemExit(f"{STATE_PATH} exists: run teardown first.")
    state = fresh_state()
    state["partial"] = True
    state["cards_baseline"] = sorted(card_ids())
    STATE_PATH.write_text(json.dumps(state, indent=1))
    for key in ("expense_log", "customer_interviews", "reading_log", "home_tasks"):
        spec = dict(COLLECTIONS[key])
        spec["linked"] = []
        state["collections"][key] = build_collection(key, spec, {})
        print(f"collection {key} (unlinked): {state['collections'][key]}")
        STATE_PATH.write_text(json.dumps(state, indent=1))
    state["contents_built"] = True
    STATE_PATH.write_text(json.dumps(state, indent=1))
    return state


def load():
    """The ids of the built world: {quests, goals, collections, docs, team_context, pivots}."""
    if not STATE_PATH.exists():
        raise SystemExit("no world built: run `runner.py setup` first")
    return json.loads(STATE_PATH.read_text())


# ---------------------------------------------------------------------------------------------
# Snapshot + diff: the side-effect detector the runner and the judge share.
# ---------------------------------------------------------------------------------------------

def entry_values(entry):
    values = entry.get("fieldValues") or entry.get("field_values") or {}
    return json.loads(json.dumps(values, default=str, sort_keys=True))


def all_tasks():
    status, body = api("GET", "/api/assistant-tasks", params={"limit": 200})
    tasks = body.get("tasks") if status == 200 and isinstance(body, dict) else []
    return [t for t in tasks if t.get("task_id") or t.get("id")]


def task_id_of(task):
    return task.get("task_id") or task.get("id")


def world_task_ids(world):
    """Tasks that belong to the eval: on a world quest, or carrying the tag. Tasks other people or
    lanes create on the shared dev account are NOT ours and are never diffed or deleted.

    A quest's recurring "Autopilot pass" (task_kind ``autopilot``) is excluded too: the dev lane's
    poller files one for every opted-in quest on its own schedule, so it is not a chat side effect.
    Counting it failed correct cases, and deleting it in ``revert`` only made the lane file another
    one before the next case. ``arm_autopilot`` schedules it a month out, so it never runs mid-eval."""
    quest_ids = set(world["quests"].values())
    return {task_id_of(t): (t.get("text") or "")[:160] for t in all_tasks()
            if (t.get("goal_id") in quest_ids or TAG in str(t.get("text") or ""))
            and t.get("task_kind") != AUTOPILOT_PASS_KIND}


def conversation_tasks(conv_id):
    """Tasks a chat turn queued (delegation), found by the conversation they came from."""
    return [t for t in all_tasks() if t.get("conv_id") == conv_id]


def delete_conversation_tasks(conv_id):
    """Delete every task row a chat conversation left behind, INCLUDING the finished ``quest_op``
    row an inline Quest write files (kind quest_op, result "Added the experiment ...").
    ``all_tasks`` never lists those (the API hides them unless ``include_quest_ops``), so the
    per-case cleanup missed them and every later turn on that quest was shown the earlier case's
    write as history (quest_activity_context's task-output channel). Returns how many it deleted."""
    status, body = api("GET", "/api/assistant-tasks",
                       params={"conv_id": conv_id, "include_quest_ops": "true", "limit": 200})
    tasks = body.get("tasks") if status == 200 and isinstance(body, dict) else []
    deleted = 0
    for task in tasks or []:
        tid = task_id_of(task)
        if tid and task.get("conv_id") in (None, conv_id):
            deleted += api("DELETE", f"/api/assistant-tasks/{tid}")[0] == 200
    return deleted


def experiments_of(qid):
    """The quest's Experiments section rows (research questions), keyed by id. The chat's
    create/update helpers write here, not to quest fields, so without this the diff reported
    "no changes" for a correct experiment write and the judge scored it as a claimed-but-unperformed
    write."""
    status, body = api("GET", f"/api/quests/{qid}/research-questions")
    rows = body.get("research_questions") if status == 200 and isinstance(body, dict) else []
    return {str(r.get("research_question_id") or r.get("id")): {
        "question": r.get("question"), "status": r.get("status"), "context": r.get("context"),
        "hypothesis": r.get("hypothesis"), "expectation": r.get("expectation"),
        "hypotheses": " | ".join(str(h.get("statement") or h.get("text") or h) if isinstance(h, dict) else str(h)
                                 for h in (r.get("hypotheses") or [])) or None,
        "purpose": r.get("purpose") or None,
        "conclusion": r.get("conclusion"), "start_date": r.get("start_date"),
        "end_date": r.get("end_date"), "kind": r.get("kind"),
        "linked_goal_ids": sorted(r.get("linked_goal_ids") or []),
        "linked_collection_ids": sorted(r.get("linked_collection_ids") or [])}
        for r in rows or [] if r.get("research_question_id") or r.get("id")}


def snapshot(world=None):
    """Everything a chat turn could change, keyed so diff() can say precisely what moved."""
    world = world or load()
    snap = {"quests": {}, "collections": {}, "all_quest_ids": [], "all_collection_ids": [],
            "tasks": world_task_ids(world)}
    mine = {q.get("quest_id"): q for q in list_quests()}
    snap["all_quest_ids"] = sorted(mine)
    listed = list_collections()
    snap["all_collection_ids"] = sorted(c.get("id") for c in listed)
    # The listing is cached server-side and can show an OLD collection only on the second read;
    # diff() reports a creation only when the collection's own created_at is after this moment.
    snap["taken_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    snap["collection_created_at"] = {
        c.get("id"): str(c.get("createdAt") or c.get("created_at") or "") for c in listed}
    for key, qid in world["quests"].items():
        state = (mine.get(qid) or {}).get("state") or quest_state(qid)
        goals = goals_of(qid)
        snap["quests"][key] = {
            "outcome": state.get("outcome"),
            "current_state": state.get("current_state"),
            "acceptance_criteria": state.get("acceptance_criteria"),
            "preferences": state.get("preferences") or None,
            "purpose": state.get("purpose") or None,
            # timeline_days is the fifth field the chat's update_quest_fields helper accepts, so a
            # case can ask for it and the diff has to be able to see it move.
            "timeline_days": state.get("timeline_days"),
            "autopilot_mode": ((state.get("autopilot") or {}).get("mode")
                               if isinstance(state.get("autopilot"), dict) else None),
            "measurable_outcomes": [
                (m.get("text"), bool(m.get("completed"))) for m in
                (state.get("measurable_outcomes") or measurable_outcomes_of(qid))],
            "notes": {str(n.get("id")): str(n.get("text")) for n in notes_of(qid)},
            "experiments": experiments_of(qid),
            "goals": {
                (g.get("id") or g.get("goal_id")): {
                    "name": g.get("name") or g.get("title"), "period": g.get("period"),
                    "completed": bool(g.get("completed")),
                    # The chat's create_goal helper takes name/description/target_date and has NO
                    # criteria parameter (it always files a MONTH period), so a goal it creates
                    # carries its measurable detail in ``description`` and its day in
                    # ``scheduled_date``. Both are captured or no case could assert on them.
                    "criteria": g.get("criteria"),
                    "description": g.get("description"),
                    "scheduled_date": g.get("scheduled_date"),
                    "deadline": g.get("deadline")} for g in goals},
        }
    meta_by_id = {c.get("id"): c for c in listed}
    snap["collection_meta"] = {}
    for key, cid in world["collections"].items():
        snap["collections"][key] = {
            e.get("id"): entry_values(e) for e in entries_of(cid) if e.get("id")}
        c = meta_by_id.get(cid) or {}
        snap["collection_meta"][key] = {
            "name": c.get("name"),
            # The listing speaks camelCase (``customFields``); reading only ``custom_fields`` made
            # every schema change, including a write that wiped a collection's fields, invisible.
            "fields": sorted(str(f.get("id") or f.get("name")) for f in (
                c.get("customFields") or c.get("custom_fields") or c.get("fields") or []))}
    return snap


def collection_predates(snap, cid, since):
    """True when ``cid``'s own created_at is before ``since``: it only surfaced in a stale listing."""
    created = (snap.get("collection_created_at") or {}).get(cid)
    if not created or not since:
        return False
    try:
        c = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
        t = datetime.datetime.fromisoformat(since.replace("Z", "+00:00"))
        if c.tzinfo is None:
            c = c.replace(tzinfo=datetime.timezone.utc)
        return c < t
    except ValueError:
        return False


def diff(before, after):
    """Human-readable list of changes between two snapshots (empty list = no side effects)."""
    changes = []
    for key, now in after["quests"].items():
        was = before["quests"].get(key, {})
        for field in ("outcome", "current_state", "acceptance_criteria", "preferences", "purpose",
                      "timeline_days", "autopilot_mode"):
            if was.get(field) != now.get(field):
                changes.append(f"quest[{key}].{field}: {was.get(field)!r} -> {now.get(field)!r}")
        if was.get("measurable_outcomes") != now.get("measurable_outcomes"):
            changes.append(f"quest[{key}].measurable_outcomes: {was.get('measurable_outcomes')} "
                           f"-> {now.get('measurable_outcomes')}")
        was_notes = was.get("notes", {})
        for nid, text in now["notes"].items():
            if nid not in was_notes:
                changes.append(f"quest[{key}] NOTE ADDED: {text[:200]!r}")
            elif was_notes[nid] != text:
                changes.append(f"quest[{key}] NOTE EDITED: {was_notes[nid][:80]!r} -> {text[:200]!r}")
        for nid, text in was_notes.items():
            if nid not in now["notes"]:
                changes.append(f"quest[{key}] NOTE REMOVED: {text[:200]!r}")
        was_exp = was.get("experiments", {})
        for xid, exp in now.get("experiments", {}).items():
            if xid not in was_exp:
                changes.append(f"quest[{key}] EXPERIMENT ADDED: {exp}")
            elif was_exp[xid] != exp:
                changes.append(f"quest[{key}] EXPERIMENT CHANGED {was_exp[xid].get('question')!r}: "
                               f"{was_exp[xid]} -> {exp}")
        for xid, exp in was_exp.items():
            if xid not in now.get("experiments", {}):
                changes.append(f"quest[{key}] EXPERIMENT REMOVED: {exp}")
        for gid, goal in now["goals"].items():
            old = was.get("goals", {}).get(gid)
            if old is None:
                changes.append(f"quest[{key}] GOAL ADDED: {goal}")
            elif old != goal:
                changes.append(f"quest[{key}] GOAL CHANGED {old.get('name')!r}: {old} -> {goal}")
        for gid, goal in was.get("goals", {}).items():
            if gid not in now["goals"]:
                changes.append(f"quest[{key}] GOAL REMOVED: {goal}")
    for key, now in after["collections"].items():
        was = before["collections"].get(key, {})
        for eid, values in now.items():
            if eid not in was:
                changes.append(f"collection[{key}] ENTRY ADDED: {values}")
            elif was[eid] != values:
                changes.append(f"collection[{key}] ENTRY CHANGED {eid}: {was[eid]} -> {values}")
        for eid, values in was.items():
            if eid not in now:
                changes.append(f"collection[{key}] ENTRY REMOVED: {values}")
    for key, now in after.get("collection_meta", {}).items():
        was = before.get("collection_meta", {}).get(key)
        if was is not None and was != now:
            changes.append(f"collection[{key}] SCHEMA CHANGED: {was} -> {now}")
    for qid in sorted(set(after["all_quest_ids"]) - set(before["all_quest_ids"])):
        changes.append(f"QUEST CREATED outside the world: {qid}")
    for qid in sorted(set(before["all_quest_ids"]) - set(after["all_quest_ids"])):
        changes.append(f"QUEST DELETED: {qid}")
    for cid in sorted(set(after["all_collection_ids"]) - set(before["all_collection_ids"])):
        if collection_predates(after, cid, before.get("taken_at")):
            continue
        changes.append(f"COLLECTION CREATED: {cid}")
    for cid in sorted(set(before["all_collection_ids"]) - set(after["all_collection_ids"])):
        changes.append(f"COLLECTION DELETED: {cid}")
    for tid, text in after["tasks"].items():
        if tid not in before["tasks"]:
            changes.append(f"TASK QUEUED {tid}: {text!r}")
    return changes


def card_ids():
    """Ids of the account's (non-managed) context cards. Chat turns LEARN cards (topic cards and a
    card per conversation) that persist across conversations and would leak one case into the
    next, so the runner and teardown delete the ones created since the baseline."""
    status, body = api("GET", "/api/cards")
    cards = body.get("cards") if status == 200 and isinstance(body, dict) else []
    return {c.get("id") for c in cards if c.get("id")}


def cards_created_since(baseline, current=None):
    """The card ids that exist now but were not in ``baseline``, sorted.

    ``baseline`` is the set captured at ``setup()`` and never refreshed, so after each sweep this is
    exactly "the cards this run created and has not yet removed". ``None`` yields nothing: with no
    baseline the sweep cannot tell a card the run created from one that pre-dates it, and it must
    then delete nothing rather than guess (see the README's known limits). Never raises."""
    if baseline is None:
        return []
    try:
        return sorted((card_ids() if current is None else set(current)) - set(baseline))
    except Exception:  # noqa: BLE001
        return []


# How long the sweep waits for a turn's card writes to SETTLE. The card updater runs in a background
# daemon thread in the brain and finishes after the HTTP response the case already read, so a write
# can land after the case's own sweep and be inherited by the next case. The wait is short: it only
# has to outlast one cheap model call plus the write.
CARD_SETTLE_QUIET_SECONDS = 2.0
CARD_SETTLE_TIMEOUT_SECONDS = 20.0


def settle_card_set(fetch, *, quiet_seconds=CARD_SETTLE_QUIET_SECONDS,
                    timeout=CARD_SETTLE_TIMEOUT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """Poll ``fetch`` until the card set stops changing for ``quiet_seconds``, then return it.

    Returns the last set seen, whether it went quiet or the ``timeout`` ran out (the caller sweeps
    either way: a late write is better caught on the next sweep than waited on forever). ``fetch``,
    ``sleep`` and ``clock`` are injected so this is testable with no network and no real waiting."""
    deadline = clock() + max(0.0, timeout)
    seen = fetch()
    quiet_since = clock()
    while clock() < deadline:
        if clock() - quiet_since >= quiet_seconds:
            return seen
        sleep(min(0.5, max(0.05, quiet_seconds / 4)))
        current = fetch()
        if current != seen:
            seen, quiet_since = current, clock()
    return seen


def delete_new_cards(baseline, current=None):
    """Delete cards that did not exist at setup. Returns how many were removed."""
    fresh = cards_created_since(baseline, current)
    for cid in fresh:
        api("DELETE", f"/api/cards/{cid}")
    return len(fresh)


def sweep_new_cards(baseline, *, settle=True):
    """Delete the cards this run created, after WAITING for the turn's card writes to settle.

    The per-case sweep used to fire the moment a case returned, which is before the brain's
    background card updater has finished: the write landed after the sweep and the NEXT case was
    answered from it (seen 2026-10-07, an eval card about one quest rewriting answers about
    another). Call this after judging AND again right before the next case starts; it is cheap when
    there is nothing new (one listing). Returns how many cards were removed."""
    if baseline is None:
        return 0
    current = settle_card_set(card_ids) if settle else None
    return delete_new_cards(baseline, current)


# ---------------------------------------------------------------------------------------------
# Card CONTENT restore. ``cards_created_since``/``sweep_new_cards`` above only catch a card this
# RUN created outright: they diff against the baseline by id, so a card that already existed (one
# from setup, an older topic card, or another run's leftover) is invisible to them no matter how
# much a case changed it. The brain's card learner does not only create cards -- it also APPENDS
# learned content items to cards that already exist, in particular to each world quest's own
# auto-maintained card (id ``quest-<quest_id>``, see quest-backend's ``quest_ai_quest_cards.py``:
# "the brain's async card updater cannot rewrite the quest-derived digest or drop the live
# reference -- but it CAN still ADD learned notes to the card. That is deliberate.").
#
# Found 2026-10-xx: over a day of eval runs the five world quest cards had accrued 3 to 23 learned
# items each, some outright false ("Goals added: ..." for a write that never landed) and some
# prescriptive ("reschedule/rename this existing goal rather than ..."), and a LATER case then read
# one of those as ground truth. Some pre-existing topic cards had learned world content too (one
# even stored a case's own answer, a collection total). An id-only sweep cannot see any of this: the
# id was already in the baseline before the case ever ran.
#
# The fix is to snapshot and diff FULL card dicts, not just ids, around each case: ``card_snapshot``
# right before the case starts, ``restore_cards`` after judging (once the card writes have settled)
# puts every card whose content differs back to its pre-case dict (PUT, the literal prior body) and
# deletes anything that is new. A managed quest card is restored the same way as any other: the
# digest fields (name/summary/description/keywords) are HASH-GATED on the backend (see
# ``quest_digest_hash``), so writing back a stale digest costs nothing -- the next real quest write
# re-derives it. This module does not try to synchronize with that hook mid-case; it only needs to
# win the LAST word once the case is over.
# ---------------------------------------------------------------------------------------------

def card_snapshot():
    """Every card on the account (managed cards included), keyed by id, as its full dict. About
    150 cards on the dev account: cheap to read whole, the same way ``card_ids`` already does for
    just the ids. Never raises; an unreadable store yields ``{}`` (the caller then restores
    nothing, same fail-safe as ``cards_created_since`` with no baseline)."""
    status, body = api("GET", "/api/cards", params={"include_managed": "true"})
    cards = body.get("cards") if status == 200 and isinstance(body, dict) else []
    return {c["id"]: c for c in cards if isinstance(c, dict) and c.get("id")}


def cards_to_restore(before, after):
    """Pure: given two ``card_snapshot()`` results, (new_ids, changed).

    ``new_ids`` are cards in ``after`` but not ``before`` (a case made these outright; delete
    them). ``changed`` is ``{id: before[id]}`` for every card in ``before`` whose dict in ``after``
    differs, INCLUDING one ``after`` no longer has at all (restoring a card a case happened to
    delete is the same operation as restoring one it merely edited -- write the prior dict back).
    A card untouched by the case is in neither. No network, so unit-testable with fabricated dicts
    (see ``test_card_restore.py``)."""
    before = before or {}
    after = after or {}
    new_ids = sorted(set(after) - set(before))
    changed = {cid: prior for cid, prior in before.items() if after.get(cid) != prior}
    return new_ids, changed


def restore_cards(before, *, settle=True):
    """Put the account's cards back to ``before`` (a ``card_snapshot()`` taken right before a
    case): delete anything new, write back (PUT, the literal prior dict) anything whose content
    changed. Waits for the case's card writes to settle first, same reasoning and the same
    settling helper as ``sweep_new_cards``. Returns a summary dict for the per-case report
    (``deleted``, ``restored``, ``changed_ids``); a restore worth reporting even when both counts
    are zero, since that itself says the case taught the card learner nothing. ``before=None`` (the
    snapshot at case start could not be read) is a no-op. Never raises."""
    if before is None:
        return {"deleted": 0, "restored": 0, "changed_ids": []}
    try:
        after = settle_card_set(card_snapshot) if settle else card_snapshot()
        new_ids, changed = cards_to_restore(before, after)
        for cid in new_ids:
            api("DELETE", f"/api/cards/{cid}")
        for cid, card in changed.items():
            api("PUT", f"/api/cards/{cid}", card)
        return {"deleted": len(new_ids), "restored": len(changed), "changed_ids": sorted(changed)}
    except Exception:  # noqa: BLE001 -- a restore failure must not crash the case that already ran
        return {"deleted": 0, "restored": 0, "changed_ids": [], "error": True}


def stash_learned_cards(path):
    """Isolate a run from old-run memory: write every non-managed learned card on the account to
    ``path`` (JSON, full dicts) and delete them from the account. ``unstash_learned_cards`` puts
    them back. The file is written and re-read BEFORE any delete, so a failure leaves the account
    untouched. Managed cards (the world quests' own) stay. Returns the number stashed."""
    status, body = api("GET", "/api/cards")
    cards = body.get("cards") if status == 200 and isinstance(body, dict) else None
    if cards is None:
        raise RuntimeError("cannot read /api/cards; not isolating")
    cards = [c for c in cards if isinstance(c, dict) and c.get("id")]
    with open(path, "w") as fh:
        json.dump(cards, fh)
    if len(json.load(open(path))) != len(cards):
        raise RuntimeError("stash file did not round-trip; not deleting anything")
    for c in cards:
        api("DELETE", f"/api/cards/{c['id']}")
    return len(cards)


def unstash_learned_cards(path):
    """Undo ``stash_learned_cards``: delete whatever learned cards exist now (the run's own),
    then write each stashed card back (PUT, the upsert). Returns the number restored."""
    for cid in card_ids():
        api("DELETE", f"/api/cards/{cid}")
    n = 0
    for c in json.load(open(path)):
        status, _ = api("PUT", f"/api/cards/{c['id']}", c)
        n += status < 300
    return n


def world_quest_card_id(qid):
    """The deterministic id quest-backend's ``quest_ai_quest_cards.card_id_for`` writes for a
    quest's own auto-maintained card."""
    return f"quest-{qid}"


def managed_only_card(card):
    """``card`` with every ``content`` item NOT named in its own ``managed_items`` dropped.

    Pure: pins exactly what ``reset_world_quest_cards`` sends back for a world quest's card. A card
    with no ``managed_items`` (or none of its content items are in it) has nothing this module may
    claim is "managed", so it is returned UNCHANGED rather than stripped to nothing -- this is only
    ever called on a card that declares the contract (see ``quest_ai_quest_cards.build_quest_card``:
    ``managed_items: [LIVE_REF_ITEM_ID]``). Returns ``card`` itself (same object) when nothing
    would change, so a caller can test ``is`` to skip a needless write."""
    managed_items = set(card.get("managed_items") or [])
    content = card.get("content")
    if not managed_items or not isinstance(content, list):
        return card
    kept = [item for item in content if isinstance(item, dict) and item.get("id") in managed_items]
    if kept == content:
        return card
    return {**card, "content": kept}


def reset_world_quest_cards(world=None):
    """Strip every LEARNED content item off each world quest's own managed card, so ``reset`` is a
    clean slate for cards too, not only for quest/collection data.

    ``teardown(keep_quests=True)`` (what ``reset`` calls) never deletes a quest's card -- it is the
    quest's OWN card, not something the eval created -- so a quest's learned items otherwise survive
    every reset indefinitely. Scoped to just the five world quest cards (a case's own drift on the
    rest of the account's cards is ``restore_cards``'s job, run per case, not reset's). Returns how
    many cards had something stripped. Never raises."""
    world = world or load()
    changed = 0
    for qid in (world.get("quests") or {}).values():
        cid = world_quest_card_id(qid)
        try:
            status, card = api("GET", f"/api/cards/{cid}")
            if status != 200 or not isinstance(card, dict):
                continue
            stripped = managed_only_card(card)
            if stripped is not card:
                api("PUT", f"/api/cards/{cid}", stripped)
                changed += 1
        except Exception:  # noqa: BLE001 -- one bad card must not abort the rest of reset()
            continue
    return changed


# ---------------------------------------------------------------------------------------------
# Decision-request cleanup: a turn that proposes a change (a quest-field write, a quest-command
# confirm) PARKS it on an "Asks for you" decision-request instead of running it. Left open, these
# (a) clutter the eval account's ask list across runs and (b) leak into later cases: an open card
# rides live_context into the NEXT turn on the same quest (quest-backend's
# ``render_pending_proposals_block``), and an open field-edit proposal makes
# ``propose_quest_field_edit``'s de-duplication treat a later case's identical suggestion as
# already asked, so it never raises its own card. See ``cancel_own_asks.py`` for why these are
# CANCELLED rather than declined (a decline feeds a 7-day per-field suppression memory and the
# capability-grant downgrade fold; see that script's docstring for the exact mechanism).
# ---------------------------------------------------------------------------------------------

#: Executable kinds this cleanup must never touch. ``machine_quest_creation`` is the world's OWN
#: quest-creation lifecycle (``world_asks.json``, ``approve_own_asks.py``, ``--decline-asks``),
#: with its own 30-day re-ask memory; it is never something a case's chat turn raises.
NEVER_CANCEL_EXECUTABLE_KINDS = frozenset({"machine_quest_creation"})


def open_decisions():
    """Every OPEN decision-request visible to the eval account. ``GET .../decisions/for-user``
    already scopes to the caller (assigned to it or created by it), so this can never return
    another account's asks."""
    status, body = api("GET", "/api/teams/decisions/for-user")
    if status != 200:
        return []
    return body if isinstance(body, list) else (body or {}).get("decisions") or []


def cancellable_decision_ids(rows, pending_asks=None):
    """Of ``rows`` (decision-request dicts carrying at least ``decision_id`` and ``executable``),
    those safe to cancel: a real decision id, not a quest-creation ask, and not one of the world's
    own still-pending quest-creation asks (belt and braces; those already fail the kind check,
    since a pending ask has no executable yet). Pure: no network, so it is unit-testable with
    fabricated rows and an explicit ``pending_asks`` set.
    """
    if pending_asks is None:
        pending_asks = set(json.loads(ASKS_PATH.read_text()).values()) if ASKS_PATH.exists() else set()
    out = []
    for row in rows:
        did = row.get("decision_id")
        kind = (row.get("executable") or {}).get("kind")
        if did and did not in pending_asks and kind not in NEVER_CANCEL_EXECUTABLE_KINDS:
            out.append(did)
    return out


def tagged_quest_decision_ids(rows, is_tagged_quest):
    """Of ``rows``, those whose ``quest_id`` ``is_tagged_quest`` (a ``quest_id -> bool`` callable)
    confirms carries the eval tag. A row naming no quest, or a quest ``is_tagged_quest`` cannot
    confirm (deleted, or never tagged), is left alone rather than guessed into a sweep: better to
    under-clean than to touch a decision that might not be this harness's. Pure: the caller
    supplies the lookup (the real one asks the backend; a test passes a dict's ``get``).
    """
    out = []
    for row in rows:
        qid, did = row.get("quest_id"), row.get("decision_id")
        if qid and did and is_tagged_quest(qid):
            out.append(did)
    return out


def sweep_cancel_candidates():
    """Everything ``runner.py cleanup-asks`` would touch: open decisions, anywhere on the eval
    account, tied to a ZZQEVAL-tagged quest. Unlike the per-case cleanup (scoped by the case's own
    fresh ``conv_id``), a leftover from an earlier, already-torn-down conversation has no conv_id
    to match, so this keys on the QUEST itself via ``tagged_quest_decision_ids``. Returns dicts
    with enough to report: ``decision_id``, ``quest_id``, ``kind`` (the executable kind), ``summary``.
    """
    rows_by_id = {r.get("decision_id"): r for r in open_decisions() if r.get("decision_id")}
    safe_ids = set(cancellable_decision_ids(rows_by_id.values()))
    safe_rows = [r for did, r in rows_by_id.items() if did in safe_ids]
    tag_cache = {}

    def is_tagged(qid):
        if qid not in tag_cache:
            state = quest_state(qid)
            tag_cache[qid] = TAG in str(state.get("acceptance_criteria") or "")
        return tag_cache[qid]

    keep = set(tagged_quest_decision_ids(safe_rows, is_tagged))
    return [{"decision_id": r["decision_id"], "quest_id": r.get("quest_id"),
            "kind": (r.get("executable") or {}).get("kind"), "summary": (r.get("summary") or "")[:160]}
            for r in safe_rows if r["decision_id"] in keep]


def cancel_decisions(decision_ids):
    """DEV ONLY: clear open decision-requests this harness raised, through ``cancel_own_asks.py``
    inside a quest-backend checkout (same pattern as ``approve_own_asks``: backend's own
    interpreter, backend directory as cwd, its own dev-environment safety check). Best-effort: a
    cleanup failure is printed and swallowed, never raised, since it must not fail a case's own
    run or a sweep over something else that could not be reached. Returns the script's per-id
    {decision_id, ok, reason?} rows (empty list on total failure)."""
    import subprocess
    if not decision_ids:
        return []
    python = os.environ.get("QUAL_BACKEND_PYTHON") or str(BACKEND_DIR / "venv" / "bin" / "python3")
    script = Path(__file__).resolve().parent / "cancel_own_asks.py"
    try:
        proc = subprocess.run([python, str(script), *decision_ids], cwd=BACKEND_DIR,
                              capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001 -- cleanup is a courtesy, never a gate
        print(f"  WARN cancel-own-asks could not run: {e}")
        return []
    results = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0:
        print(f"  WARN cancel-own-asks failed (exit {proc.returncode}): "
              f"{(proc.stderr or '').strip().splitlines()[-1:]}")
    return results


def ground_truth(quest_keys=None):
    """Plain-text description of what was SEEDED, for the judge to verify facts against."""
    keys = quest_keys or list(QUESTS)
    lines = [f"Today is {TODAY.isoformat()}."]
    # Entry dates were seeded relative to the day the contents were BUILT. Listing them from the
    # run's today instead shifted every date by the days in between (a run one day after a build
    # told the judge an entry dated 2 Oct was dated 3 Oct, and the judge failed a correct reply).
    seeded_on = TODAY
    try:
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        if state.get("contents_seeded_on"):
            seeded_on = datetime.date.fromisoformat(state["contents_seeded_on"])
    except Exception:  # noqa: BLE001 — fall back to today, as before
        seeded_on = TODAY
    if seeded_on != TODAY:
        lines.append(f"(The data below was seeded on {seeded_on.isoformat()}; its dates are listed "
                     "as stored.)")
    for key in keys:
        q = QUESTS[key]
        lines.append(f"\n== QUEST '{key}' ==")
        lines.append(f"Autopilot mode: {AUTOPILOT_BY_QUEST.get(key, 'off')}"
                     + ("  (the AI MAY apply a field change this user asked for)"
                        if AUTOPILOT_BY_QUEST.get(key) != "off" else
                        "  (the AI may NOT apply any change to this quest's own fields: outcome, "
                        "current_state, acceptance_criteria, preferences, timeline_days, "
                        "measurable outcomes. The correct behaviour is to propose it for approval "
                        "and say it was not applied.)"))
        lines.append(f"Outcome: {q['outcome']}")
        lines.append(f"Acceptance criteria: {q['acceptance_criteria']}")
        lines.append(f"Current state: {q['current_state']}")
        lines.append("Measurable outcomes: " + "; ".join(q["measurable_outcomes"]))
        lines.append("Goals: " + "; ".join(f"{g[0]} ({g[1]})" for g in q["goals"]))
        for text, _ in q["notes"]:
            lines.append(f"Quest note: {text}")
        for title, content, _ in q.get("docs", []):
            lines.append(f"Quest doc '{title}': {content}")
        for ckey, c in COLLECTIONS.items():
            if key not in c["linked"]:
                continue
            lines.append(f"Collection '{c['name']}' ({ckey}, {c['type']}"
                         f"{', ' + c['habit_type'] if c.get('habit_type') else ''}):")
            for days, _hour, values in c["entries"]:
                day = (seeded_on - datetime.timedelta(days=days)).isoformat()
                shown = "completed" if c["type"] == "habit" else json.dumps(values)
                lines.append(f"  - {day}: {shown}")
    lines.append(f"\nTeam context: {TEAM_CONTEXT['content']}")
    lines.append(CHAT_CAPABILITIES)
    return "\n".join(lines)


#: What the in-app chat can and cannot do, from quest-backend's sandbox helper allow-list
#: (``_SANDBOX_HELPER_NAMES`` in ``app/api/endpoints/ai_commands.py``) and the prompt that
#: documents it (``app/prompts/ai_commands.yaml``). The judge is given this verbatim so it never
#: credits a claim the surface cannot make, and never marks down an honest "I cannot do that".
#: Keep it in step with that allow-list; it is the single place the datasets' fairness rests on.
CHAT_CAPABILITIES = """
== WHAT THIS CHAT SURFACE CAN AND CANNOT DO (verified against quest-backend's sandbox allow-list) ==
It acts by generating Python that calls named helpers. It CAN:
  quests: get_recent_quests, get_quest_details, update_quest_fields(quest_id, fields) for the five
    allowed fields ONLY (outcome, current_state, preferences, acceptance_criteria, timeline_days),
    add_quest_measurable_outcome, update_outcome_progress, complete_quest, archive_quest,
    search_quest_context, get_quest_notes (READ), add_quest_note(quest_id, text) (appends a
    note to the quest's thread in the person's own words; added to the sandbox 2026-10-06)
  goals: create_goal(quest_id, name, description, target_date, ...), complete_goal, delete_goal,
    delete_goals_by_scope, create_team_goal, assign_goal, post_goal_update, set_goal_parent,
    get_todays_actions, get_my_rundown
  habits and collections: log_habit, create_habit, find_collection, get_collection_entries,
    add_collection_entry, get_insights, mark_insight_acted_on
  reflection and reviews: save_daily_reflection, get_latest_reflection, parse_daily_plan,
    create_daily_goals, get_period_review_stats, save_period_review, create_period_goals
  tasks: create_assistant_task, cancel_assistant_task, get_task_results
  experiments (the quest's Experiments section): get_research_questions (READ), search_research_questions(quest_id, query),
    create_research_question(quest_id, question, ...), update_research_question(quest_id, id, fields). No delete.
  raw Mongo on five collections only: quests, goals, collections, entries, users. A raw write
    (update_one/update_many/delete_one) on ``quests`` is refused by a guard; raw writes on goals,
    collections and entries go through, so renaming a goal, editing a goal's criteria, editing or
    deleting an entry and adding a field to a collection are all possible, by raw write.
  also: get_user_settings, update_user_settings (whitelisted keys only), get_goal_updates,
    get_latest_completions, get_latest_entries, get_quest_progress
  log_habit(habit_id) with no value records a habit completion but NO collection entry; only
    log_habit(habit_id, value) or add_collection_entry writes an entry row.
It CANNOT, so an honest statement of the limit is the RIGHT answer and a claim of success is a
failure:
  - set a quest's ``purpose``, ``strategies`` or any field outside the five allowed ones;
    update_quest_fields rejects the key outright.
  - give a goal made by create_goal a week period or a ``criteria`` field: create_goal always files
    a MONTH period and carries measurable detail in ``description`` (``target_date`` keeps the
    requested day). create_period_goals(period="week", ...) is the route that files week goals.
  - replace or delete an existing measurable outcome (it can only ADD one).
  - send email. There is no send or draft-mail helper on this surface.
  - move money, reach the user's filesystem, or search the web through the code path (a web search,
    if it happens at all, is a gather/read step, not a helper call).
"""


def revert(before, after, world=None):
    """Best-effort undo of a case's side effects so the next case starts from the seeded world.
    Returns True when a fresh snapshot equals ``before`` again; otherwise the caller should
    ``reset()`` (which needs no new approvals)."""
    world = world or load()
    for tid in set(after["tasks"]) - set(before["tasks"]):
        api("DELETE", f"/api/assistant-tasks/{tid}")
    for key, now in after["quests"].items():
        was = before["quests"].get(key, {})
        qid = world["quests"][key]
        for field in ("outcome", "current_state", "acceptance_criteria", "timeline_days"):
            if was.get(field) != now.get(field) and was.get(field) is not None:
                api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": was[field]})
        # Measurable outcomes are a list the chat can append to (add_quest_measurable_outcome), so
        # put the seeded list back wholesale rather than leaving an extra one behind for the next case.
        if was.get("measurable_outcomes") and was["measurable_outcomes"] != now.get("measurable_outcomes"):
            api("PUT", f"/api/quests/{qid}/measurable-outcomes", {
                "outcomes": [{"text": text, "completed": bool(done)}
                             for text, done in was["measurable_outcomes"]]})
        # preferences and purpose are seeded empty: a case that set one is cleared (best effort).
        for field in ("preferences", "purpose"):
            if was.get(field) is None and now.get(field) is not None:
                api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": ""})
        for nid in set(now["notes"]) - set(was.get("notes", {})):
            api("DELETE", f"/api/quests/{qid}/notes/{nid}")
        # Experiments: delete the ones a case created, put an edited one's details back.
        was_exp = was.get("experiments", {})
        for xid, exp in now.get("experiments", {}).items():
            if xid not in was_exp:
                api("DELETE", f"/api/quests/{qid}/research-questions/{xid}")
            elif was_exp[xid] != exp:
                old = was_exp[xid]
                # The quest's own default question keeps its question text and context (the API
                # refuses them), so restore only what a default row lets anyone change.
                keys = (("hypothesis", "status", "conclusion", "start_date", "end_date")
                        if old.get("kind") == "default" else
                        ("question", "context", "hypothesis", "status", "conclusion",
                         "start_date", "end_date"))
                api("PATCH", f"/api/quests/{qid}/research-questions/{xid}", {
                    k: ("" if old.get(k) is None and k in ("hypothesis", "conclusion") else old.get(k))
                    for k in keys if old.get(k) is not None or k in ("hypothesis", "conclusion")})
        for gid, goal in now["goals"].items():
            old = was.get("goals", {}).get(gid)
            if old is None:
                api("DELETE", f"/api/planning/goals/{gid}")
            elif old != goal:
                api("PUT", f"/api/planning/goals/{gid}", {
                    "name": old.get("name"), "completed": old.get("completed"),
                    "criteria": old.get("criteria"), "description": old.get("description")})
    for key, now in after["collections"].items():
        was = before["collections"].get(key, {})
        cid = world["collections"][key]
        for eid, values in now.items():
            if eid not in was:
                api("DELETE", f"/api/data/entries/{eid}", params={"collection_id": cid})
            elif was[eid] != values:
                api("PUT", f"/api/data/entries/{eid}", {"field_values": was[eid]},
                    params={"collection_id": cid})
    # collections the chat created (an explicit "create a collection" request) are deleted
    for cid in set(after["all_collection_ids"]) - set(before["all_collection_ids"]):
        api("DELETE", f"/api/data/collections/{cid}")
    again = snapshot(world)
    return not diff(before, again)


# ---------------------------------------------------------------------------------------------
# Teardown / reset
# ---------------------------------------------------------------------------------------------

def restore_quest_fields(key, qid):
    """Put the editable quest fields back to their seeded values (human edit route)."""
    spec = QUESTS[key]
    for field, value in (("outcome", spec["outcome"]),
                         ("current_state", spec["current_state"]),
                         ("timeline_days", spec["timeline_days"]),
                         ("acceptance_criteria", f"{spec['acceptance_criteria']} ({TAG})")):
        api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": value})


def teardown(keep_quests=False, decline_asks=False):
    """Delete the world and PROVE it is gone. ``keep_quests`` leaves the five quests (whose
    creation needed a person's approval) in place with their fields restored, so a re-setup needs
    no new approvals; everything else is deleted and verified either way."""
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else fresh_state()
    report = []

    # Sweep by TAG too, so a half-built or orphaned world is still cleaned.
    tagged_quests = set(state["quests"].values())
    for q in list_quests():
        s = q.get("state") or {}
        if TAG in str(s.get("acceptance_criteria") or ""):
            tagged_quests.add(q.get("quest_id"))
    tagged_collections = set(state["collections"].values())
    for c in list_collections():
        if TAG in str(c.get("description") or ""):
            tagged_collections.add(c.get("id"))

    # Any assistant task on a world quest (e.g. a delegated turn) goes too, so the dev runner lane
    # never executes work queued by an eval turn.
    for t in all_tasks():
        if t.get("goal_id") in tagged_quests or TAG in str(t.get("text") or ""):
            tid = task_id_of(t)
            report.append(("task", tid, api("DELETE", f"/api/assistant-tasks/{tid}")[0]))

    for qid in tagged_quests:
        for goal in goals_of(qid):
            gid = goal.get("id") or goal.get("goal_id")
            report.append(("goal", gid, api("DELETE", f"/api/planning/goals/{gid}")[0]))
        if keep_quests:
            for note in notes_of(qid):
                if note.get("id"):
                    report.append(("note", note["id"], api(
                        "DELETE", f"/api/quests/{qid}/notes/{note['id']}")[0]))
    for cid in tagged_collections:
        report.append(("collection", cid, api("DELETE", f"/api/data/collections/{cid}")[0]))
    for title, ref in state.get("docs", {}).items():
        qid = state["quests"].get(ref["quest"])
        if qid:
            report.append(("quest doc", ref["id"],
                           api("DELETE", f"/api/quests/{qid}/context-entries/{ref['id']}")[0]))
    if state.get("team_context"):
        report.append(("team context", state["team_context"], api(
            "DELETE", f"/api/teams/{QUEST_TEAM}/context-entries/{state['team_context']}")[0]))
    if keep_quests:
        for key, qid in state["quests"].items():
            restore_quest_fields(key, qid)
    else:
        for qid in tagged_quests:
            report.append(("quest", qid, api("DELETE", f"/api/quests/{qid}")[0]))
    if decline_asks and ASKS_PATH.exists():
        for key, decision in json.loads(ASKS_PATH.read_text()).items():
            report.append(("ask", decision, api(
                "POST", f"/api/teams/decisions/{decision}/resolve", {"resolution": "decline"})[0]))
        ASKS_PATH.unlink()
    removed = delete_new_cards(state.get("cards_baseline"))
    if state.get("cards_baseline") is not None:
        print(f"deleted {removed} context card(s) learned during eval turns")
    for kind, ident, status in report:
        print(f"delete {kind} {ident}: HTTP {status}")

    print("\n--- verifying it is really gone ---")
    gone = True
    mine = {q.get("quest_id") for q in list_quests()}
    if keep_quests:
        print(f"quests kept on purpose: {len(tagged_quests & mine)}/{len(tagged_quests)}")
        for key, qid in state["quests"].items():
            now = quest_state(qid)
            for field, want in (("outcome", QUESTS[key]["outcome"]),
                                ("current_state", QUESTS[key]["current_state"])):
                if now.get(field) != want:
                    print(f"quest {key}.{field} NOT restored: {now.get(field)!r}")
                    gone = False
            leftover = notes_of(qid)
            if leftover:
                print(f"quest {key} still has {len(leftover)} note(s)")
                gone = False
    else:
        still = sorted(tagged_quests & mine)
        print(f"quests still on the account: {len(still)} {still}")
        gone &= not still
        status, body = api("GET", f"/api/teams/{QUEST_TEAM}/quests")
        board = {q.get("quest_id") for q in body} if isinstance(body, list) else set()
        print(f"quests still on the team board: {sorted(tagged_quests & board)}")
        gone &= not (tagged_quests & board)
    for cid in sorted(tagged_collections):
        status, _ = api("GET", f"/api/data/collections/{cid}")
        if status not in (404, 403):
            print(f"collection {cid} -> {status} (STILL THERE)")
            gone = False
    left = [c.get("name") for c in list_collections() if TAG in str(c.get("description") or "")]
    print(f"{TAG} collections still on the account: {len(left)} {left}")
    gone &= not left
    for qid in tagged_quests:
        remaining = goals_of(qid)
        if remaining:
            print(f"goals still on {qid}: {[g.get('name') for g in remaining]}")
            gone = False
    if state.get("team_context"):
        status, body = api("GET", f"/api/teams/{QUEST_TEAM}/context-entries")
        ids = {e.get("id") for e in unwrap_list(body, "entries", "items")}
        print(f"team context entry still present: {state['team_context'] in ids}")
        gone &= state["team_context"] not in ids
    pending = json.loads(ASKS_PATH.read_text()) if ASKS_PATH.exists() else {}
    if pending:
        print(f"note: {len(pending)} quest-creation ask(s) remain open for a person: {pending}")
    print("\nCLEANUP VERIFIED" if gone else "\nCLEANUP INCOMPLETE")
    if gone and STATE_PATH.exists():
        STATE_PATH.unlink()
        if keep_quests and not state.get("partial"):
            kept = fresh_state()
            kept["quests"] = state["quests"]
            kept["cards_baseline"] = state.get("cards_baseline")
            STATE_PATH.write_text(json.dumps(kept, indent=1))
    return gone


def reset():
    """Return the world to its seeded state without new approvals: teardown(keep_quests) + rebuild.

    ``teardown(keep_quests=True)`` keeps each world quest's own card (it belongs to the quest, not
    to the eval), so a learned item on one survives teardown and every prior reset indefinitely.
    ``reset_world_quest_cards`` strips those back to managed-only content here too, so a fresh reset
    is a clean slate for cards as well as quest/collection data."""
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
    if state is None or state.get("partial") or not state.get("quests"):
        raise SystemExit("reset needs a full world; run setup")
    if not teardown(keep_quests=True):
        raise SystemExit("reset aborted: cleanup incomplete")
    state = json.loads(STATE_PATH.read_text())
    build_contents(state, list(QUESTS))
    stripped = reset_world_quest_cards(state)
    print(f"stripped learned content from {stripped} world quest card(s)")


def show():
    world = load()
    print(json.dumps({k: v for k, v in world.items() if k != "pivots"}, indent=1))
    for name, p in PIVOTS.items():
        print(f"{name} [{p['quest_key']}]: {p['description']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["setup", "teardown", "reset", "show"])
    parser.add_argument("--wait", type=int, default=0,
                        help="setup: seconds to poll for a person to approve the quest asks")
    parser.add_argument("--partial", action="store_true", help="setup: smoke world, no quests")
    parser.add_argument("--approve-own-asks", action="store_true",
                        help="setup, DEV ONLY: approve the world's own quest asks via quest-backend")
    parser.add_argument("--keep-quests", action="store_true", help="teardown: keep approved quests")
    parser.add_argument("--decline-asks", action="store_true",
                        help="teardown: decline any still-open quest-creation asks")
    args = parser.parse_args()
    if args.cmd == "show":
        show()
        sys.exit(0)
    from devclient import WorldLock
    with WorldLock(f"world.py {' '.join(sys.argv[1:])}"):
        if args.cmd == "setup":
            setup_partial() if args.partial else setup(args.wait, args.approve_own_asks)
        elif args.cmd == "teardown":
            ok = teardown(args.keep_quests, args.decline_asks)
            sys.exit(0 if ok else 1)
        else:
            reset()
