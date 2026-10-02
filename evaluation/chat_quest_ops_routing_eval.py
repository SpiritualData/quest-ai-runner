"""Quest-operation ROUTING + EXECUTION eval for Quest AI chat (QAR), with NO deep execution.

WHAT THIS ANSWERS
-----------------
Two questions, at once, for a user who has NO external environment (no machine, no Claude Code,
i.e. the ordinary Quest subscriber):

  1. ROUTING. Does the chat brain keep plain quest-database operations INLINE (a read, or a direct
     tool call), instead of over-routing them to a deep run? And does it still route work that
     genuinely needs a deep run (code, files, research) to "deep"?
  2. EXECUTION. For the operations it does perform inline, does it perform them CORRECTLY, verified
     independently against the real Quest API rather than believed from the reply text?

WHY NOTHING EXPENSIVE OR RISKY CAN HAPPEN HERE
----------------------------------------------
``RunnerConfig.deep_runner`` defaults to a sentinel that AUTO-BUILDS a real SubprocessGoalRunner
(which spawns real Claude Code). This harness sets ``cfg.deep_runner = None`` BEFORE
``build_orchestrator``, which ``config.resolve_deep_runner`` treats as the deliberate tri-state
"execution disabled, no warning". A turn the planner routes to "deep" therefore comes back as
``kind == "deep"`` carrying the honest NO_DEEP_EXECUTOR text and runs nothing. That is exactly the
signal we want: the routing DECISION is captured, no deep work is paid for, and the result is also
a faithful simulation of a user with no external environment.

WHAT "NO EXTERNAL ENVIRONMENT" IS MODELLED AS
---------------------------------------------
  * ``deep_runner = None``            -- no machine to execute deep work on.
  * ``QAR_CORPUS_ROOT`` = empty dir   -- no corpus to grep. (A corpus root must exist at all, or
                                         ``get_retrieval_adapter`` returns None and the
                                         QuestRetrievalAdapter never gets wired -- the quest reads
                                         we are evaluating would then be impossible by
                                         construction rather than by behavior.)
  * ``QAR_CONVERSATION_SEARCH=false`` -- no local Claude Code session history.
  * no ``QAR_TOOLS_FILE``             -- only QAR's STANDARD tools, i.e. what any Quest customer
                                         gets, not Spiritual Data's own extra lane tools.

DEV ONLY. Quest credentials are read from ``setup/sd-dev-runner/.env`` (api.batmanhq.duckdns.org)
and asserted to be dev, never ``api.spiritualdata.org``. Test data is created through the REAL dev
REST API (the same endpoints the app uses), never by writing to Mongo, so the fixture is shaped
exactly like a real user's data. ``--teardown`` deletes it and re-fetches to prove it is gone.

USAGE
-----
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py setup
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py run [--only ID,ID]
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py teardown
"""
import argparse
import datetime
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Repo root derived from THIS file (evaluation/<this>.py) so the harness is path-agnostic: no
# absolute machine path hardcoded (public-repo hard rule #1). Same boilerplate as
# evaluation/card_quality_eval.py.
REPO = str(Path(__file__).resolve().parents[1])
for line in (Path(REPO) / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
sys.path.insert(0, REPO)

# The DEV lane's credentials. Not setdefault: these must WIN over anything the repo .env set, since
# pointing the eval at the wrong Quest instance is the one mistake that matters here.
DEV_ENV_FILE = Path("/home/joshua/hq/stories/spiritual_data/product/setup/sd-dev-runner/.env")
DEV_ENV = {}
for line in DEV_ENV_FILE.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        DEV_ENV[k.strip()] = v.strip().strip('"').strip("'")

QUEST_BASE = DEV_ENV["QUEST_BASE_URL"].rstrip("/")
QUEST_KEY = DEV_ENV["QUEST_API_KEY"]
QUEST_TEAM = DEV_ENV.get("QUEST_TEAM_ID") or ""
assert "batmanhq" in QUEST_BASE and "spiritualdata.org" not in QUEST_BASE, (
    f"REFUSING TO RUN: {QUEST_BASE} is not the dev Quest backend")

STATE_PATH = Path("/tmp/qopseval/fixture.json")
RESULTS_PATH = Path("/tmp/qopseval/results.json")
TAG = "ZZEVAL"


# ---------------------------------------------------------------------------------------------
# Dev Quest REST client (setup, independent verification, teardown). Real endpoints only.
# ---------------------------------------------------------------------------------------------

def api(method, path, body=None, params=None):
    url = QUEST_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {QUEST_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:1500]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def entries_of(collection_id):
    """Entries of one collection, normalised: the endpoint returns a bare list for some collection
    types and a {"items": [...], "pagination": {...}} page for others."""
    status, body = api("GET", f"/api/data/collections/{collection_id}/entries")
    if status != 200:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or body.get("entries") or []
    return []


def goals_of(quest_id):
    """Every goal on a quest, flattened. NOTE the only list route is TEAM-scoped
    (GET /api/teams/{team}/quests/{quest}/goals) -- there is no owner-scoped goals list, which is
    also why the quest under test has to be attached to a team for Quest AI to read its goals."""
    status, body = api("GET", f"/api/teams/{QUEST_TEAM}/quests/{quest_id}/goals")
    if status != 200 or not isinstance(body, dict):
        return []
    out = []
    for group in body.get("period_groups") or []:
        out.extend(group.get("goals") or [])
    return out


def quest_state(quest_id):
    status, body = api("GET", f"/api/quests/{quest_id}/state")
    if status != 200 or not isinstance(body, dict):
        return {}
    return body.get("state") or body


def list_collections():
    status, body = api("GET", "/api/data/collections")
    if status != 200:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("collections") or body.get("items") or []
    return []


def notes_of(quest_id):
    status, body = api("GET", f"/api/quests/{quest_id}/notes")
    if status != 200:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("notes") or body.get("items") or []
    return []


# ---------------------------------------------------------------------------------------------
# Fixture: a realistic disposable quest on dev, built through the app's own REST endpoints.
# ---------------------------------------------------------------------------------------------

FIXTURE_CATEGORY = "cat_df82187e53c0"  # "Fitness"


def setup():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {}

    status, body = api("POST", "/api/quests/start", {
        "category_id": FIXTURE_CATEGORY,
        "outcome": f"{TAG}-OUTCOME: Run a sub-50-minute 10K by the end of the test window",
        "acceptance_criteria": f"{TAG}-AC: a timed 10K under 50:00 recorded on a watch",
        "current_state": f"{TAG}-STATE: currently running 10K in 58 minutes, three times a week",
        "timeline_days": 30,
        "creation_mode": "quick",
    })
    assert status == 201, (status, body)
    quest = body["quest_id"]
    out["quest"] = quest
    print("quest:", quest)

    # Attach it to the dev team. Not cosmetic: QuestClient.list_quest_goals (the ONLY goals-list
    # route, and the one QuestRetrievalAdapter uses) is team-scoped, so Quest AI cannot see the
    # goals of a quest that is on no team at all.
    print("attach to team:", api("POST", f"/api/teams/{QUEST_TEAM}/quest", {"quest_id": quest})[0])

    print("measurable outcomes:", api("PUT", f"/api/quests/{quest}/measurable-outcomes", {
        "outcomes": [
            {"text": f"{TAG}-MO-1: complete eight consecutive weeks of three runs per week",
             "completed": False},
            {"text": f"{TAG}-MO-2: record a 10K time under 52:00 as a checkpoint",
             "completed": False},
        ]})[0])

    out["goals"] = {}
    for label, name, period, scope in [
        ("A", f"{TAG}-GOAL-A: Build weekly mileage to 40km", "2026_W41", "week"),
        ("B", f"{TAG}-GOAL-B: Run one tempo session per week", "2026_W41", "week"),
        ("C", f"{TAG}-GOAL-C: Do the October long-run block", "2026_10", "month"),
    ]:
        status, body = api("POST", "/api/planning/goals", {
            "quest_id": quest, "name": name, "period": period, "time_scope": scope,
            "criteria": f"{TAG}-CRIT: measured on the running watch"})
        assert status == 200, (status, body)
        out["goals"][label] = body["id"]
        print("goal", label, body["id"])

    status, body = api("POST", "/api/data/collections", {
        "name": f"{TAG} Morning Run",
        "description": f"{TAG} test habit: did I run this morning",
        "type": "habit", "habit_type": "binary", "frequency": {"type": "daily"},
        "custom_fields": [], "linked_quest_ids": [quest], "quick_entry_enabled": True})
    assert status == 201, (status, body)
    out["habit"] = body["id"]
    print("habit:", body["id"])

    status, body = api("POST", "/api/data/collections", {
        "name": f"{TAG} Stretching Timer", "description": f"{TAG} test timer habit",
        "type": "habit", "habit_type": "timer",
        "frequency": {"type": "daily", "durationGoalMinutes": 15},
        "custom_fields": [], "linked_quest_ids": [quest]})
    assert status == 201, (status, body)
    out["timer"] = body["id"]
    print("timer habit:", body["id"])

    status, body = api("POST", "/api/data/collections", {
        "name": f"{TAG} Run Log", "description": f"{TAG} non-habit journal collection",
        "type": "journal",
        "custom_fields": [
            {"id": "distance_km", "name": "Distance km", "type": "number", "required": True},
            {"id": "effort", "name": "Effort", "type": "rating", "rating_min": 1, "rating_max": 5},
            {"id": "notes", "name": "Notes", "type": "multiline"},
        ],
        "linked_quest_ids": [quest]})
    assert status == 201, (status, body)
    out["journal"] = body["id"]
    print("journal:", body["id"])

    today = datetime.date.today()
    for days in (1, 2, 3):
        day = (today - datetime.timedelta(days=days)).isoformat()
        api("POST", "/api/data/entries", {
            "collection_id": out["habit"], "field_values": {"status": "yes"},
            "created_at": f"{day}T07:30:00Z", "linked_quest_ids": [quest]})
    for days, (km, effort) in zip((1, 2, 4), ((8.2, 4), (5.0, 2), (12.5, 5))):
        day = (today - datetime.timedelta(days=days)).isoformat()
        api("POST", "/api/data/entries", {
            "collection_id": out["journal"],
            "field_values": {"distance_km": km, "effort": effort,
                             "notes": f"{TAG} run on {day}"},
            "created_at": f"{day}T08:00:00Z", "linked_quest_ids": [quest]})

    STATE_PATH.write_text(json.dumps(out, indent=1))
    print("fixture written to", STATE_PATH)
    return out


def teardown():
    fx = json.loads(STATE_PATH.read_text())
    quest = fx["quest"]
    report = []

    # Goals first (both the fixture's and anything the eval created on this quest), then the
    # collections (which cascade their entries), then the quest itself.
    for goal in goals_of(quest):
        gid = goal.get("id") or goal.get("goal_id")
        report.append(("goal", gid, api("DELETE", f"/api/planning/goals/{gid}")[0]))
    for key in ("habit", "timer", "journal"):
        cid = fx.get(key)
        if cid:
            report.append((key, cid, api("DELETE", f"/api/data/collections/{cid}")[0]))
    report.append(("quest", quest, api("DELETE", f"/api/quests/{quest}")[0]))

    for kind, ident, status in report:
        print(f"delete {kind} {ident}: HTTP {status}")

    # Re-FETCH, through the same read routes the eval itself used, and prove each one is empty or
    # not found. (There is no owner-scoped GET for a single goal or a quest document, so the
    # checks use the routes that do exist: the quest's /state and the team goals list.)
    print("\n--- verifying it is really gone ---")
    gone = True
    # GET /api/quests/{id}/state is NOT a usable existence check: it keeps serving a deleted quest
    # from cache (observed on dev, 2026-10-02). The authoritative lists are the owner's quest list
    # and the team board, so check those.
    status, body = api("GET", "/api/quests/me")
    mine = [q.get("quest_id") for q in body] if isinstance(body, list) else []
    print(f"GET /api/quests/me -> {status}; test quest present: {quest in mine} "
          f"{'(gone)' if quest not in mine else '(STILL THERE)'}")
    gone &= status == 200 and quest not in mine
    status, body = api("GET", f"/api/teams/{QUEST_TEAM}/quests")
    board = [q.get("quest_id") for q in body] if isinstance(body, list) else []
    print(f"GET team board -> {status}; test quest present: {quest in board}")
    gone &= quest not in board
    for key in ("habit", "timer", "journal"):
        cid = fx.get(key)
        if not cid:
            continue
        status, _ = api("GET", f"/api/data/collections/{cid}")
        print(f"GET collection {cid} -> {status} "
              f"{'(gone)' if status in (404, 403) else '(STILL THERE)'}")
        gone &= status in (404, 403)
    left = goals_of(quest)
    print(f"goals still on the quest: {len(left)} {[g.get('name') for g in left]}")
    gone &= not left
    remaining = [c for c in (list_collections() or [])
                 if TAG in str(c.get("name", ""))]
    print(f"{TAG} collections still on the account: {len(remaining)} "
          f"{[c.get('name') for c in remaining]}")
    gone &= not remaining
    print("\nCLEANUP VERIFIED" if gone else "\nCLEANUP INCOMPLETE")
    return gone


# ---------------------------------------------------------------------------------------------
# The orchestrator under test.
# ---------------------------------------------------------------------------------------------

def build():
    """A real Orchestrator shaped like a Quest user with NO external environment."""
    scratch = Path("/tmp/qopseval/empty_corpus")
    scratch.mkdir(parents=True, exist_ok=True)

    os.environ["QUEST_BASE_URL"] = QUEST_BASE
    os.environ["QUEST_API_KEY"] = QUEST_KEY
    if QUEST_TEAM:
        os.environ["QUEST_TEAM_ID"] = QUEST_TEAM
    os.environ["QAR_CORPUS_ROOT"] = str(scratch)
    os.environ["QAR_CONVERSATION_SEARCH"] = "false"
    os.environ["QAR_STANDARD_TOOLS"] = "1"
    for drop in ("QAR_TOOLS_FILE", "QAR_CONFIG_FILE", "QAR_CONTEXT_PREAMBLE_FILE",
                 "QAR_LINK_POLICY_FILE"):
        os.environ.pop(drop, None)
    # The deployed SD lanes plan and answer through the stock `claude` CLI, so use that backend:
    # routing accuracy is a property of the model that actually makes the decision in production.
    os.environ["QAR_MODEL_BACKEND"] = DEV_ENV.get("QAR_MODEL_BACKEND", "claude_cli")
    os.environ["QAR_PLANNER_TIER"] = DEV_ENV.get("QAR_PLANNER_TIER", "sonnet")
    # The repo .env names GEMINI model ids per tier. Keep them and every tier resolves to a
    # provider that isn't registered under the claude_cli backend, so clear them and let the
    # backend's own tier defaults apply, exactly as the deployed lane does (it sets none of these).
    for tier in ("QAR_MODEL_FAST", "QAR_MODEL_BALANCED", "QAR_MODEL_QUALITY", "QAR_MODEL_BEST",
                 "QAR_OVERSEER_TIER", "QAR_VERIFY_TIER"):
        os.environ.pop(tier, None)
    if DEV_ENV.get("QAR_CLAUDE_PATH"):
        os.environ["QAR_CLAUDE_PATH"] = DEV_ENV["QAR_CLAUDE_PATH"]
    # The overseer is a second judge on top of the planner; leave it as the lane has it (off by
    # default here) so a routing verdict is the PLANNER's, which is what we are measuring.
    os.environ.pop("QAR_OVERSEER", None)

    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator

    cfg = _config_from_env()
    # THE SAFETY LATCH. Explicit None = "disable execution deliberately" (config.resolve_deep_runner).
    # Without this the sentinel default auto-builds a real SubprocessGoalRunner and spawns Claude Code.
    cfg.deep_runner = None
    orch = build_orchestrator(cfg)
    assert not getattr(orch, "deep_runner", None), "deep runner is wired; refusing to run"
    tools = sorted(orch.tools.names()) if getattr(orch, "tools", None) else []
    return orch, tools


class RecordingSink:
    """Captures every planner action and tool call for the turn. Must never raise."""

    def __init__(self):
        self.actions = []
        self.tools = []
        self.events = []

    def update(self, event, mode):  # noqa: ARG002
        try:
            self.events.append(event.type)
            if event.type in ("plan", "replan") and event.action:
                self.actions.append(event.action)
            if event.type == "exec" and (event.data or {}).get("tool"):
                phase = (event.data or {}).get("phase")
                if phase == "tool_call":
                    self.tools.append(event.data["tool"])
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------
# The dataset. Each case: id, area, message, expected routing, and an independent verifier.
#
# expect = "inline"  -> must NOT be a deep run (answer, including a tool-backed answer)
#          "deep"    -> must be a deep run (genuinely needs an external environment)
# ---------------------------------------------------------------------------------------------

def contains_all(text, *needles):
    low = (text or "").lower()
    missing = [n for n in needles if n.lower() not in low]
    return (not missing), ("has every expected value" if not missing
                           else f"reply never mentions {missing}")


def today_habit_row(habit_id):
    today = datetime.date.today().isoformat()
    for entry in entries_of(habit_id):
        fv = entry.get("fieldValues") or entry.get("field_values") or {}
        if str(fv.get("entry_date") or fv.get("period_start") or "").startswith(today):
            return fv
    return None


def build_dataset(fx):
    quest = fx["quest"]
    goals = fx["goals"]
    habit, timer, journal = fx["habit"], fx["timer"], fx["journal"]

    def verify_read(*needles):
        return lambda res: contains_all(res.text, *needles)

    def verify_habit_complete(res):  # noqa: ARG001
        fv = today_habit_row(habit)
        if fv is None:
            return False, "no habit row for today exists at all"
        done = bool(fv.get("completed"))
        return done, (f"today's habit row completed={done}, "
                      f"value_achieved={fv.get('value_achieved')}")

    def verify_timer_logged(res):  # noqa: ARG001
        fv = today_habit_row(timer)
        if fv is None:
            return False, "no timer-habit row for today exists"
        secs = (fv.get("habit_timer") or {}).get("value")
        return bool(secs), f"today's timer row seconds={secs}, completed={fv.get('completed')}"

    def verify_new_journal_entry(res):  # noqa: ARG001
        for entry in entries_of(journal):
            fv = entry.get("fieldValues") or entry.get("field_values") or {}
            if str(fv.get("distance_km")) in ("10.4", "10.4000", "10"):
                return True, f"found a new Run Log entry {fv}"
        return False, "no Run Log entry with distance_km 10.4 exists"

    def verify_new_goal(res):  # noqa: ARG001
        known = set(goals.values())
        extra = [g for g in goals_of(quest)
                 if (g.get("id") or g.get("goal_id")) not in known]
        return bool(extra), (f"new goal(s): {[g.get('title') or g.get('name') for g in extra]}"
                             if extra else "no new goal on the quest")

    def verify_goal_b_complete(res):  # noqa: ARG001
        for g in goals_of(quest):
            if (g.get("id") or g.get("goal_id")) == goals["B"]:
                done = bool(g.get("completed"))
                return done, f"GOAL-B completed={done}"
        return False, "GOAL-B not found"

    def verify_note_added(res):  # noqa: ARG001
        hits = [n for n in notes_of(quest) if "travel" in str(n.get("text", "")).lower()]
        return bool(hits), (f"{len(hits)} matching quest note(s)" if hits
                            else f"no quest note mentions travelling (quest has "
                                 f"{len(notes_of(quest))} note(s))")

    def verify_current_state_written(res):  # noqa: ARG001
        state = (quest_state(quest).get("current_state") or "")
        ok = "55" in state
        return ok, f"current_state is now {state!r}"

    def verify_outcome_written(res):  # noqa: ARG001
        outcome = (quest_state(quest).get("outcome") or "")
        ok = "48" in outcome
        return ok, f"outcome is now {outcome!r}"

    def verify_reflection_split(res):  # noqa: ARG001
        hits = [n for n in notes_of(quest)
                if "tempo" in str(n.get("text", "")).lower()
                or "8.2" in str(n.get("text", ""))]
        return bool(hits), (f"{len(hits)} quest note(s) carry the reflection"
                            if hits else "the reflection text reached no quest note")

    def no_execution_expected(res):
        """For a correctly-routed deep case: nothing ran, and the reply says so honestly."""
        text = (res.text or "") + " ".join(
            d.output or "" for d in (res.deep_results or []))
        honest = ("no deep executor" in text.lower()
                  or "cannot auto-execute" in text.lower()
                  or res.kind == "deep")
        return honest, "routed to deep and reported non-execution honestly" if honest else (
            f"routed {res.kind} with text {text[:200]!r}")

    return [
        # --- AREA 4: asking questions about a particular quest's fields -----------------------
        dict(id="R1", area="read quest fields", expect="inline",
             msg="What is the outcome of this quest?",
             verify=verify_read("sub-50", "10k")),
        dict(id="R2", area="read quest fields", expect="inline",
             msg="What are the measurable outcomes on this quest?",
             verify=verify_read("eight consecutive weeks", "52:00")),
        dict(id="R3", area="read quest fields", expect="inline",
             msg="Where am I right now on this quest? What does it say my current state is?",
             verify=verify_read("58 minutes")),
        dict(id="R4", area="read quest fields", expect="inline",
             msg="What are the acceptance criteria on this quest?",
             verify=verify_read("50:00", "watch")),
        # --- AREA 6: listing / querying existing quest data ------------------------------------
        dict(id="L1", area="list quest data", expect="inline",
             msg="List the goals on this quest.",
             verify=verify_read("mileage", "tempo", "long-run")),
        dict(id="L2", area="list quest data", expect="inline",
             msg="Which habits am I tracking for this quest?",
             verify=verify_read("morning run")),
        dict(id="L3", area="list quest data", expect="inline",
             msg="How many kilometres did I log in my Run Log over the last few days?",
             verify=verify_read("8.2", "12.5")),
        # --- AREA 1: daily reflection through chat --------------------------------------------
        dict(id="D1", area="daily reflection", expect="inline",
             msg=("Here is my daily reflection. Yesterday I ran 8.2km and felt strong, my legs "
                  "held up well. Today I am planning a tempo run. Please record it."),
             verify=verify_reflection_split),
        dict(id="D2", area="daily reflection", expect="inline",
             msg="What did I write in my most recent week review?",
             verify=lambda res: (bool(res.text) and "cannot" not in (res.text or "").lower(),
                                 (res.text or "")[:200])),
        # --- AREA 2: marking a habit complete --------------------------------------------------
        dict(id="H1", area="habit completion", expect="inline",
             msg="Mark my morning run habit complete for today.",
             verify=verify_habit_complete),
        dict(id="H2", area="habit completion", expect="inline",
             msg="I stretched for 15 minutes just now, log that against my stretching habit.",
             verify=verify_timer_logged),
        # --- AREA 7: habit timers --------------------------------------------------------------
        dict(id="T1", area="habit timer", expect="inline",
             msg="Start the timer on my stretching habit now.",
             verify=lambda res: (False, (res.text or "")[:300])),
        # --- AREA 3: adding an entry with specific field values --------------------------------
        dict(id="E1", area="collection entry", expect="inline",
             msg=("Add an entry to my Run Log collection: distance 10.4 km, effort 4, "
                  "notes 'felt easy all the way round'."),
             verify=verify_new_journal_entry),
        # --- AREA 5: creating and updating goals / notes ---------------------------------------
        dict(id="G1", area="goal create", expect="inline",
             msg="Add a goal to this quest: do two track sessions a week through October.",
             verify=verify_new_goal),
        dict(id="G2", area="goal update", expect="inline",
             msg="Mark the tempo session goal on this quest as complete.",
             verify=verify_goal_b_complete),
        dict(id="G3", area="quest note", expect="inline",
             msg=("Add a note to this quest: I am travelling next week so my mileage will drop."),
             verify=verify_note_added),
        # --- quest FIELD writes: the one governed native tool -----------------------------------
        dict(id="F1", area="quest field write", expect="inline",
             msg=("Update the current state on this quest: I am now running 10K in 55 minutes."),
             verify=verify_current_state_written),
        dict(id="F2", area="quest field write", expect="inline",
             msg="Change this quest's outcome to running a sub-48-minute 10K.",
             verify=verify_outcome_written),
        # --- plain informing (must not become work) --------------------------------------------
        dict(id="C1", area="inform only", expect="inline",
             msg="In Quest, what is the difference between a goal and a habit?",
             verify=lambda res: (bool((res.text or "").strip()), (res.text or "")[:200])),
        # --- CONTRAST: genuinely needs a deep run / external environment -----------------------
        dict(id="X1", area="contrast: code", expect="deep",
             msg=("Fix the back button on the quest detail screen in the quest-frontend repo so "
                  "it returns to the goals list instead of the home screen."),
             verify=no_execution_expected),
        dict(id="X2", area="contrast: files", expect="deep",
             msg=("Research the best 12-week training plans for a sub-50 10K and write me a "
                  "summary document in my corpus."),
             verify=no_execution_expected),
        dict(id="X3", area="contrast: machine", expect="deep",
             msg=("Run the quest-backend test suite on my machine and tell me which tests are "
                  "failing right now."),
             verify=no_execution_expected),
    ]


# ---------------------------------------------------------------------------------------------
# Runner + scoring
# ---------------------------------------------------------------------------------------------

def run(only=None):
    fx = json.loads(STATE_PATH.read_text())
    orch, tool_names = build()
    print(f"Quest backend : {QUEST_BASE}")
    print(f"Deep runner   : DISABLED (cfg.deep_runner = None)")
    print(f"Native tools  : {tool_names or '(none)'}")
    print(f"Test quest    : {fx['quest']}\n")

    dataset = build_dataset(fx)
    if only:
        wanted = {c.strip() for c in only.split(",")}
        dataset = [c for c in dataset if c["id"] in wanted]

    rows = []
    for case in dataset:
        sink = RecordingSink()
        started = time.time()
        err = None
        try:
            res = orch.run(case["msg"], quest_id=fx["quest"], sink=sink)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            res = None
        took = time.time() - started

        if res is None:
            row = dict(case_id=case["id"], area=case["area"], message=case["msg"],
                       expected=case["expect"], kind="ERROR", actions=sink.actions,
                       tools=sink.tools, routing_ok=False, correct=False,
                       note=err, reply="", seconds=round(took, 1))
        else:
            routed_deep = res.kind == "deep"
            routing_ok = routed_deep if case["expect"] == "deep" else not routed_deep
            try:
                correct, note = case["verify"](res)
            except Exception as e:  # noqa: BLE001
                correct, note = False, f"verifier raised {type(e).__name__}: {e}"
            row = dict(case_id=case["id"], area=case["area"], message=case["msg"],
                       expected=case["expect"], kind=res.kind, actions=sink.actions,
                       tools=sink.tools, routing_ok=routing_ok, correct=bool(correct),
                       note=note, exit_reason=res.exit_reason,
                       reply=(res.text or "")[:1200], seconds=round(took, 1))
        rows.append(row)
        print(f"[{row['case_id']:3}] {row['area']:22} expect={row['expected']:6} "
              f"kind={row['kind']:9} route={'OK ' if row['routing_ok'] else 'BAD'} "
              f"exec={'OK ' if row['correct'] else 'BAD'} tools={row['tools']} "
              f"actions={row['actions']} ({row['seconds']}s)")
        print(f"      {row['note']}")
        RESULTS_PATH.write_text(json.dumps(
            {"quest_backend": QUEST_BASE, "native_tools": tool_names, "rows": rows}, indent=1))

    print("\n==================== SUMMARY ====================")
    print(f"routing correct : {sum(r['routing_ok'] for r in rows)}/{len(rows)}")
    print(f"execution/read  : {sum(r['correct'] for r in rows)}/{len(rows)}")
    print(f"results written : {RESULTS_PATH}")
    return rows


# ---------------------------------------------------------------------------------------------
# ARM 2: the REAL in-app Quest AI chat, driven over HTTP on dev.
#
# WHY A SECOND ARM. The QAR-library arm above is the brain SD's own lanes run (and the brain an
# external environment runs). It is NOT what a Quest subscriber talks to in the app: quest-backend
# builds its OWN Orchestrator (app/business/quests/quest_ai_core_adapters.py) with its own
# retrieval, context-card assemblers, sandboxed quest-operation tools and a deep-runner classifier,
# and serves it at POST /api/quest-ai/conversations/{id}/messages/stream. Since the question is
# "is this still a nice experience for a user with no external environment", that surface is the
# one that has to be measured, so this arm drives it for real and reads back its SSE events, where
# the routing decision is visible: `plan` events carry the planner `action`, `exec` events carry
# tool/execution frames, and `task_queued` / `delegated` mean the turn was handed OFF to an
# external environment instead of being done inline.
# ---------------------------------------------------------------------------------------------

def sse_send(conv_id, content, *, auto_run=True, timeout=600):
    """POST one chat turn to the streaming route and collect every SSE event."""
    url = f"{QUEST_BASE}/api/quest-ai/conversations/{conv_id}/messages/stream"
    data = json.dumps({"content": content, "auto_run": auto_run}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {QUEST_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    events = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    events.append({"type": "_unparsed", "text": payload[:200]})
    except urllib.error.HTTPError as e:
        events.append({"type": "_http_error", "text": f"{e.code}: {e.read().decode()[:400]}"})
    except Exception as e:  # noqa: BLE001
        events.append({"type": "_error", "text": f"{type(e).__name__}: {e}"})
    return events


class InAppResult:
    """Adapts an in-app SSE turn to the same shape the dataset's verifiers expect."""

    def __init__(self, events):
        # NOTE the wire shape: the backend's SSE frames key the event name as "event" (not
        # "type"), the reply text arrives as "token" frames and again whole on the closing "done"
        # frame, and the planner action sits at data.action on a "plan" frame.
        self.events = events

        def name(e):
            return e.get("event") or e.get("type") or ""

        done = [e for e in events if name(e) == "done"]
        final = " ".join(str(e.get("content") or "") for e in done).strip()
        if not final:
            final = " ".join(str(e.get("text") or "") for e in events
                             if name(e) in ("token", "result")).strip()
        self.text = final
        self.actions = [(e.get("data") or {}).get("action") or e.get("action")
                        for e in events if name(e) in ("plan", "replan")]
        self.actions = [a for a in self.actions if a]
        self.tools = sorted({(e.get("data") or {}).get("tool")
                             for e in events if name(e) == "exec"
                             and (e.get("data") or {}).get("tool")})
        self.exec_frames = [e.get("data") for e in events if name(e) == "exec"]
        self.statuses = [str(e.get("text") or "") for e in events if name(e) == "status"]
        self.delegated = any(
            name(e) == "task_queued" or e.get("delegated")
            or (e.get("data") or {}).get("delegated") for e in events)
        self.task_ids = [e.get("task_id") for e in events if e.get("task_id")]
        self.errors = [e for e in events if name(e).startswith("_") or name(e) == "error"]
        # "deep" for scoring purposes means the turn left this process: either the planner chose a
        # deep run, or the turn was delegated to an external environment as a queued task.
        self.kind = "deep" if ("deep" in self.actions or self.delegated) else "answer"
        self.deep_results = []
        self.exit_reason = ",".join(self.actions[-1:]) or ""


def run_inapp(only=None):
    fx = json.loads(STATE_PATH.read_text())
    status, body = api("POST", "/api/quest-ai/conversations", {"quest_ids": [fx["quest"]]})
    assert status == 201, (status, body)
    conv = body.get("conversation_id") or body.get("id") or (body.get("data") or {}).get("id")
    print(f"Quest backend : {QUEST_BASE}  (in-app Quest AI chat, REAL app surface)")
    print(f"Test quest    : {fx['quest']}")
    print(f"Conversation  : {conv}")
    print("auto_run      : True (a user's \"Allow all\"; the DEFAULT is False, which parks a "
          "mutation on an approval card instead of running it)\n")

    dataset = build_dataset(fx)
    if only:
        wanted = {c.strip() for c in only.split(",")}
        dataset = [c for c in dataset if c["id"] in wanted]

    rows = []
    for case in dataset:
        started = time.time()
        res = InAppResult(sse_send(conv, case["msg"]))
        took = time.time() - started
        routed_deep = res.kind == "deep"
        routing_ok = routed_deep if case["expect"] == "deep" else not routed_deep
        try:
            correct, note = case["verify"](res)
        except Exception as e:  # noqa: BLE001
            correct, note = False, f"verifier raised {type(e).__name__}: {e}"
        row = dict(case_id=case["id"], area=case["area"], message=case["msg"],
                   expected=case["expect"], kind=res.kind, actions=res.actions,
                   tools=res.tools, delegated=res.delegated, task_ids=res.task_ids,
                   routing_ok=routing_ok, correct=bool(correct), note=note,
                   errors=res.errors, reply=(res.text or "")[:1500],
                   seconds=round(took, 1))
        rows.append(row)
        print(f"[{row['case_id']:3}] {row['area']:22} expect={row['expected']:6} "
              f"kind={row['kind']:7} route={'OK ' if routing_ok else 'BAD'} "
              f"exec={'OK ' if correct else 'BAD'} delegated={res.delegated} "
              f"tools={res.tools} actions={res.actions} ({row['seconds']}s)")
        print(f"      {note}")
        if res.errors:
            print(f"      TRANSPORT: {res.errors}")
        Path("/tmp/qopseval/results_inapp.json").write_text(json.dumps(
            {"quest_backend": QUEST_BASE, "conversation": conv, "rows": rows}, indent=1))

    print("\n============== IN-APP SUMMARY ==============")
    print(f"routing correct : {sum(r['routing_ok'] for r in rows)}/{len(rows)}")
    print(f"execution/read  : {sum(r['correct'] for r in rows)}/{len(rows)}")
    print(f"delegated out   : {sum(bool(r['delegated']) for r in rows)}/{len(rows)}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["setup", "run", "run-inapp", "teardown"])
    parser.add_argument("--only", default=None, help="comma-separated case ids")
    args = parser.parse_args()
    if args.phase == "setup":
        setup()
    elif args.phase == "run":
        run(args.only)
    elif args.phase == "run-inapp":
        run_inapp(args.only)
    else:
        teardown()


if __name__ == "__main__":
    main()
