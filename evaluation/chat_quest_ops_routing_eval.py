"""Quest-operation ROUTING + EXECUTION eval for Quest AI chat (QAR), with NO deep execution paid
for by this harness (the QAR-library arm disables the real deep runner; the in-app arm lets the
real dev backend decide routing, but any resulting task is cancelled immediately).

WHAT THIS ANSWERS
-----------------
Two questions, at once, for a user who has NO external environment (no machine, no Claude Code,
i.e. the ordinary Quest subscriber):

  1. ROUTING. Does the chat brain keep plain quest-database operations INLINE (a read, or a direct
     tool call), instead of over-routing them to a deep run? And does it still route work that
     genuinely needs a deep run (code, files, research) to "deep"? Routing on the in-app arm is
     classified honestly: a planner "deep" action is NOT the same thing as the turn leaving this
     process (see ROUTE CLASSIFICATION below).
  2. EXECUTION. For the operations it does perform inline, does it perform them CORRECTLY, verified
     independently against the real Quest API rather than believed from the reply text? And, for
     the default auto_run=false arm, is a mutation correctly PARKED on an approval and only applied
     after a yes?

ROUTE CLASSIFICATION (in-app arm)
----------------------------------
quest-backend's chat brain has THREE things that can happen to a turn, and only one of them is a
deep run in the quest-ai-runner sense:
  * ``delegated``   -- the turn left this process: a background task was enqueued (SSE
                        ``{"event": "delegated", "task_id": ...}``), either as a short-circuit
                        decision or mid-turn (``task_queued`` internally). This is the only
                        "deep" in the sense this eval's ``expect="deep"`` cases mean.
  * ``inline_write`` -- QuestCommandRunner generated and ran MUTATING Python in-process (an
                        ``exec`` frame with ``data.phase == "code"`` and ``data.mutating is True``).
  * ``inline_code``  -- QuestCommandRunner generated and ran READ-ONLY Python in-process (exec
                        frames present, none mutating).
  * ``answer``       -- no exec frames, no delegation: a plain LLM answer (or the inline text
                        runner).
A turn's ``route`` is the first of these that applies, checked in that order. ``expect="inline"``
passes when ``route != "delegated"``; ``expect="deep"`` passes only when ``route == "delegated"``.
The raw planner fact (``"deep" in actions``) is kept as its own column, separately, because a
planner "deep" choice that resolves to ``inline_write``/``inline_code`` is correct honest behavior,
not a routing miss (see quest-ai-runner's CLAUDE.md "Deep-run status lines must be true").

WHY NOTHING EXPENSIVE OR RISKY CAN HAPPEN ON THE QAR-LIBRARY ARM (``run``)
---------------------------------------------------------------------------
``RunnerConfig.deep_runner`` defaults to a sentinel that AUTO-BUILDS a real SubprocessGoalRunner
(which spawns real Claude Code). This harness sets ``cfg.deep_runner = None`` BEFORE
``build_orchestrator``, which ``config.resolve_deep_runner`` treats as the deliberate tri-state
"execution disabled, no warning". A turn the planner routes to "deep" therefore comes back as
``kind == "deep"`` carrying the honest NO_DEEP_EXECUTOR text and runs nothing.

THE IN-APP ARM (``run-inapp``) DOES REAL WRITES AND CAN REALLY DELEGATE
-------------------------------------------------------------------------
``run-inapp`` drives the real dev backend's own Orchestrator over HTTP, which is the surface a
Quest subscriber actually talks to. A delegated turn really enqueues a real assistant task on dev;
this harness cancels every such task immediately (``POST /api/assistant-tasks/{id}/undo``) and
again in teardown as a safety net. A mutation that lands inline really writes to the fixture quest;
teardown deletes the fixture quest and its collections, and separately snapshots/restores the
account-level daily-reflection entry for today (writes to it are NOT scoped to the fixture quest).

WHAT "NO EXTERNAL ENVIRONMENT" IS MODELLED AS (QAR-library arm only)
----------------------------------------------------------------------
  * ``deep_runner = None``            -- no machine to execute deep work on.
  * ``QAR_CORPUS_ROOT`` = empty dir   -- no corpus to grep.
  * ``QAR_CONVERSATION_SEARCH=false`` -- no local Claude Code session history.
  * no ``QAR_TOOLS_FILE``             -- only QAR's STANDARD tools, i.e. what any Quest customer
                                         gets, not Spiritual Data's own extra lane tools.

DEV ONLY, AND PATH-AGNOSTIC (public-repo hard rule #1)
---------------------------------------------------------
Quest credentials are read from the file named by the required env var ``QAR_EVAL_DEV_ENV_FILE``
(for this deployment, export it as ``<product>/setup/sd-dev-runner/.env`` in your shell -- never
hardcode that path into this file). The URL in that file is asserted to be a dev host, never
``api.spiritualdata.org``. The fixture's quest category comes from ``QAR_EVAL_CATEGORY_ID`` if
set, else this harness looks one up live via ``GET /api/categories/all`` (a category whose name
contains "fitness"); it refuses to run if neither source finds one. Scratch output goes under
``QAR_EVAL_OUT_DIR`` (default ``/tmp/qopseval``). Test data is created through the REAL dev REST
API (the same endpoints the app uses), never by writing to Mongo -- WITH ONE EXCEPTION, documented
next.

QUEST CREATION IS GATED SINCE 2026-10-06 -- THE FACTORY FALLBACK
-------------------------------------------------------------------
quest-backend's ``machine_quest_gate`` now holds any quest an API key asks to create
(``POST /api/quests/start`` returns 202 ``pending_approval``), and an API key can never approve
that hold itself (only decline it). This harness still tries the real REST route first every time;
on a 202 it declines the held decision (so it never sits there unresolved) and then falls back to,
in order:
  1. ``QAR_EVAL_QUEST_ID`` if set -- reuse this existing (already-approved) quest id verbatim. A
     manual escape hatch for one debugging run, not for ``--repeat > 1`` (teardown deletes it).
  2. ``QAR_EVAL_QUEST_FACTORY`` -- a full shell command, run with ``shell=True`` and cwd
     ``QAR_EVAL_QUEST_FACTORY_CWD``, whose stdout's last line is the new quest id. This is how an
     operator seeds the fixture quest directly through storage instead of the gated REST route;
     quest-backend's ``scripts/checks/eval_fixture_quest.py`` is the reference implementation, e.g.
     ``QAR_EVAL_QUEST_FACTORY='venv/bin/python3 scripts/checks/eval_fixture_quest.py create --category-id <id> --api-key "$QUEST_API_KEY"'``.
     The factory creates a BARE quest; this harness then sets outcome/acceptance_criteria/
     current_state on it through the normal human-actor field-edit route, same as every other
     field write in ``setup()``.
Teardown deletes the fixture quest through the real REST route as always, and additionally runs
``QAR_EVAL_QUEST_DELETE`` (a command template containing the literal ``{quest_id}``, same
cwd var) when set, as a second, redundant removal path for whichever quests were created outside
REST. Neither factory env var is read unless quest creation actually hits the 202 gate.

USAGE
-----
    export QAR_EVAL_DEV_ENV_FILE=/path/to/sd-dev-runner/.env   # once, in your shell
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py setup
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py selftest
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py run [--only ID,ID]
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py run-inapp [--only ID,ID]
        [--repeat N] [--auto-run on|off]
    .venv/bin/python3 evaluation/chat_quest_ops_routing_eval.py teardown

``run-inapp`` OWNS its own fixture lifecycle: each of its ``--repeat`` passes (default 2) is a
fresh ``setup`` -> run every selected case in its own conversation -> ``teardown`` with cleanup
verification, so passes can never share (and corrupt) a fixture. The standalone ``setup``/
``teardown`` phases stay available for manual poking (``selftest`` uses them the same way).
``--auto-run off`` drives the approval-card arm: WRITE-kind cases are sent with ``auto_run=false``
(the app's real default), checked for being correctly held, then approved with "Yes, go ahead." in
the same conversation and re-checked; non-write cases are skipped in this arm.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
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

# The DEV lane's credentials, read from a path the CALLER names (never hardcoded here -- public
# repo hard rule #1). Not setdefault: these must WIN over anything the repo .env set, since
# pointing the eval at the wrong Quest instance is the one mistake that matters here.
DEV_ENV_FILE_VAR = "QAR_EVAL_DEV_ENV_FILE"
if not os.environ.get(DEV_ENV_FILE_VAR):
    raise SystemExit(
        f"{DEV_ENV_FILE_VAR} is not set. Export it to the dev lane's .env path before running "
        f"this eval, e.g.:\n"
        f"    export {DEV_ENV_FILE_VAR}=/path/to/sd-dev-runner/.env\n"
        f"This harness never hardcodes a real machine path (public-repo hard rule #1)."
    )
DEV_ENV_FILE = Path(os.environ[DEV_ENV_FILE_VAR]).expanduser()
if not DEV_ENV_FILE.is_file():
    raise SystemExit(f"{DEV_ENV_FILE_VAR} points at a file that does not exist: {DEV_ENV_FILE}")
DEV_ENV = {}
for line in DEV_ENV_FILE.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        DEV_ENV[k.strip()] = v.strip().strip('"').strip("'")

QUEST_BASE = DEV_ENV["QUEST_BASE_URL"].rstrip("/")
QUEST_KEY = DEV_ENV["QUEST_API_KEY"]
QUEST_TEAM = DEV_ENV.get("QUEST_TEAM_ID") or ""
assert "spiritualdata.org" not in QUEST_BASE, (
    f"REFUSING TO RUN: {QUEST_BASE} looks like the production Quest backend, not dev")

OUT_DIR = Path(os.environ.get("QAR_EVAL_OUT_DIR") or "/tmp/qopseval")
STATE_PATH = OUT_DIR / "fixture.json"
RESULTS_PATH = OUT_DIR / "results.json"
TAG = "ZZEVAL"
STEP_CAP = 15  # QuestCommandRunner's read-step cap (quest-backend playbook); used to flag exhaustion.


# ---------------------------------------------------------------------------------------------
# Dev Quest REST client (setup, independent verification, teardown). Real endpoints only.
# ---------------------------------------------------------------------------------------------

def api(method, path, body=None, params=None, attempts=5):
    """One REST call. A 429 (the dev backend's shared per-account rate limit, which other
    harnesses on the same account also spend) is retried with backoff instead of being read as a
    real failure of the fixture or of a verifier."""
    for attempt in range(attempts):
        status, payload = api_once(method, path, body, params)
        if status != 429 or attempt == attempts - 1:
            return status, payload
        time.sleep(10 * (attempt + 1))
    return status, payload


def api_once(method, path, body=None, params=None):
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


def unwrap_api_response(body):
    """Unwrap the standard ``ApiResponse.success`` envelope ({"success","data","message","error"})
    when present; otherwise return body as-is. Several data/collections routes wrap their payload
    this way and a caller that reads the top level directly gets nothing back."""
    if isinstance(body, dict) and "data" in body and set(body.keys()) >= {"success", "data"}:
        return body.get("data")
    return body


def entries_of(collection_id):
    """Entries of one collection, normalised: the endpoint returns a bare list for some collection
    types and a {"items": [...], "pagination": {...}} page for others."""
    status, body = api("GET", f"/api/data/collections/{collection_id}/entries")
    if status != 200:
        return []
    body = unwrap_api_response(body)
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


def collections_all():
    """Every collection on this account, unwrapped. The route's own docstring says ``limit=0``
    means "no limit, returns all", but the query param is declared ``ge=1`` and rejects 0 with a
    422 (found live while building this harness -- a real quest-backend mismatch between the
    docstring and the validator, reported separately, not fixed here). Use a large concrete limit
    instead; a disposable eval fixture account will never hold anywhere near this many."""
    status, body = api("GET", "/api/data/collections", params={"limit": 500})
    if status != 200:
        return []
    body = unwrap_api_response(body)
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("collections") or body.get("items") or []
    return []


def list_collections():
    """Back-compat alias kept for teardown's own leftover-collections scan."""
    return collections_all()


def collections_linked_to_quest(quest_id):
    out = []
    for c in collections_all():
        linked = c.get("linked_quest_ids") or c.get("linkedQuestIds") or []
        if quest_id in linked:
            out.append(c)
    return out


def find_collection_by_system_type(system_type):
    """The ONE account-level collection with this system_type (daily_reflection, week_review, ...),
    or None. There should only ever be zero or one per account."""
    for c in collections_all():
        if c.get("system_type") == system_type or c.get("systemType") == system_type:
            return c
    return None


def notes_of(quest_id):
    status, body = api("GET", f"/api/quests/{quest_id}/notes")
    if status != 200:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("notes") or body.get("items") or []
    return []


def today_all():
    status, body = api("GET", "/api/planning/today/all")
    if status != 200 or not isinstance(body, dict):
        return {}
    return body


def habit_today_status(quest_id, habit_collection_id):
    """The habit row Today's Actions actually shows for this quest + habit, or None if the habit
    does not appear in today's groups at all (e.g. not due today, or not found)."""
    body = today_all()
    for group in body.get("quests") or []:
        if group.get("questId") != quest_id:
            continue
        for h in group.get("habits") or []:
            if (h.get("collectionId") or h.get("collection_id") or h.get("id")) == habit_collection_id:
                return h
    # Also check the standalone group, in case linkage ever drops the quest scoping.
    for group in body.get("quests") or []:
        if group.get("questId") != "__standalone__":
            continue
        for h in group.get("habits") or []:
            if (h.get("collectionId") or h.get("collection_id") or h.get("id")) == habit_collection_id:
                return h
    return None


def timer_state(habit_id):
    status, body = api("GET", f"/api/planning/habits/{habit_id}/timer")
    if status != 200 or not isinstance(body, dict):
        return {}
    return body


def set_timer(habit_id, action, **extra):
    body = {"action": action, "mode": "stopwatch"}
    if action == "start":
        body["startedAt"] = extra.get("started_at") or now_iso()
    body.update({k: v for k, v in extra.items() if k not in ("started_at",)})
    return api("POST", f"/api/planning/habits/{habit_id}/timer", body)


def now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def decisions_for_quest(quest_id):
    status, body = api("GET", "/api/teams/decisions/for-quest", params={"quest_id": quest_id})
    if status != 200 or not isinstance(body, list):
        return []
    return body


def open_field_decisions(quest_id, marker=None):
    out = []
    for d in decisions_for_quest(quest_id):
        if d.get("status") != "open":
            continue
        if d.get("kind") not in ("field_edit", "quest_command"):
            continue
        if marker is not None and marker.lower() not in json.dumps(d.get("executable") or {}).lower():
            continue
        out.append(d)
    return out


def resolve_decision(decision_id, resolution="approve", response_text=None):
    body = {"resolution": resolution}
    if response_text:
        body["response_text"] = response_text
    return api("POST", f"/api/teams/decisions/{decision_id}/resolve", body)


def get_task(task_id):
    status, body = api("GET", f"/api/assistant-tasks/{task_id}")
    if status != 200 or not isinstance(body, dict):
        return {}
    return body


def cancel_task(task_id):
    """POST /{id}/undo is the real cancel route (PATCH status=cancelled is explicitly refused by
    the backend). 404/409 both mean "nothing to cancel" (not found, or already terminal)."""
    return api("POST", f"/api/assistant-tasks/{task_id}/undo")


def resolve_category_id():
    cat_id = os.environ.get("QAR_EVAL_CATEGORY_ID")
    if cat_id:
        return cat_id
    status, body = api("GET", "/api/categories/all", params={"limit": 500})
    cats = (body or {}).get("categories") or [] if isinstance(body, dict) else []
    for c in cats:
        name = str(c.get("name") or "")
        if "fitness" in name.lower():
            found = c.get("id") or c.get("category_id")
            if found:
                return found
    raise RuntimeError(
        "QAR_EVAL_CATEGORY_ID is not set, and GET /api/categories/all (status "
        f"{status}) found no category whose name contains 'fitness'. Set "
        "QAR_EVAL_CATEGORY_ID explicitly to the category id to use for the fixture quest."
    )


# ---------------------------------------------------------------------------------------------
# Fixture: a realistic disposable quest on dev, built through the app's own REST endpoints -- with
# a factory fallback for the one step (quest CREATION) that an API key can no longer do directly
# since the 2026-10-06 machine_quest_gate. See the module docstring's "QUEST CREATION IS GATED"
# section for the full contract of QAR_EVAL_QUEST_ID / QAR_EVAL_QUEST_FACTORY[_CWD] /
# QAR_EVAL_QUEST_DELETE.
# ---------------------------------------------------------------------------------------------

def run_factory_command(command, label):
    """Run an operator-supplied shell command (from env, never hardcoded here) and return its
    stripped stdout. Raises with the command's stderr on a non-zero exit."""
    cwd = os.environ.get("QAR_EVAL_QUEST_FACTORY_CWD") or None
    result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                            timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {result.returncode}): "
                           f"stdout={result.stdout!r} stderr={result.stderr[-800:]!r}")
    return (result.stdout or "").strip()


def create_fixture_quest(category_id, outcome, acceptance_criteria, current_state, timeline_days):
    """POST /api/quests/start first, same as any ordinary caller. On 202 pending_approval (an API
    key cannot create a quest directly since 2026-10-06), decline the held decision so it never
    sits there unresolved. A repeat of the IDENTICAL request within 30 days of a decline is
    refused outright with 409 (machine_quest_gate's own fingerprint cache) rather than filing a
    new hold; both cases fall back the same way, to QAR_EVAL_QUEST_ID or QAR_EVAL_QUEST_FACTORY.
    Returns the quest id with outcome/acceptance_criteria/current_state already set, either way."""
    status, body = api("POST", "/api/quests/start", {
        "category_id": category_id, "outcome": outcome,
        "acceptance_criteria": acceptance_criteria, "current_state": current_state,
        "timeline_days": timeline_days, "creation_mode": "quick",
    })
    if status == 201:
        return body["quest_id"]
    if status not in (202, 409):
        raise AssertionError((status, body))

    # api() returns a parsed dict for a 2xx JSON body but a RAW STRING for an HTTPError body
    # (status >= 400), since it never attempts to parse an error response. Normalize here.
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    decision_id = (body or {}).get("decision_id")
    if status == 202:
        print(f"quest creation held for approval (decision {decision_id}); an API key cannot "
              f"approve it, only decline it.")
        if decision_id:
            d_status, d_body = resolve_decision(
                decision_id, "decline",
                "evaluation harness: using the operator fixture factory instead")
            print(f"declined held decision {decision_id}: HTTP {d_status} {d_body}")
    else:
        print(f"quest creation refused outright (409): an identical request was already "
              f"declined and machine_quest_gate's fingerprint cache remembers it. {body}")

    quest_id = os.environ.get("QAR_EVAL_QUEST_ID")
    if quest_id:
        print(f"using QAR_EVAL_QUEST_ID={quest_id} (manual override, not a fresh quest)")
        return quest_id

    factory_command = os.environ.get("QAR_EVAL_QUEST_FACTORY")
    if not factory_command:
        raise RuntimeError(
            "POST /api/quests/start is held for approval (machine_quest_gate, since 2026-10-06) "
            "and neither QAR_EVAL_QUEST_ID nor QAR_EVAL_QUEST_FACTORY is set. See this file's "
            "module docstring, 'QUEST CREATION IS GATED', for how to set one.")
    stdout = run_factory_command(factory_command, "QAR_EVAL_QUEST_FACTORY")
    quest_id = stdout.splitlines()[-1].strip() if stdout else ""
    if not quest_id:
        raise RuntimeError(f"QAR_EVAL_QUEST_FACTORY produced no quest id (stdout={stdout!r})")
    print(f"created fixture quest via factory: {quest_id}")

    # The factory creates a BARE quest (see eval_fixture_quest.py); set the fields it would
    # normally carry at creation through the same human-actor field route the app's own field
    # editor uses (never the AI-gated write path -- this is a plain field edit, not an AI
    # proposal). One PATCH per field: EditFieldRequest declares a `fields` dict for a multi-field
    # update, but quests/state.py's edit_field handler only ever reads `field_name`/`value` (the
    # legacy single-field shape) and silently ignores `fields` entirely, which 500s downstream on
    # a None field_name (found live while building this fallback -- a real quest-backend bug,
    # reported separately, not fixed here).
    for field_name, value in (("outcome", outcome), ("acceptance_criteria", acceptance_criteria),
                              ("current_state", current_state)):
        status, body = api("PATCH", f"/api/quests/{quest_id}/field",
                           {"fieldName": field_name, "value": value})
        assert status == 200, (field_name, status, body)
    return quest_id


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    category_id = resolve_category_id()

    quest = create_fixture_quest(
        category_id,
        outcome=f"{TAG}-OUTCOME: Run a sub-50-minute 10K by the end of the test window",
        acceptance_criteria=f"{TAG}-AC: a timed 10K under 50:00 recorded on a watch",
        current_state=f"{TAG}-STATE: currently running 10K in 58 minutes, three times a week",
        timeline_days=30)
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

    # Best-effort read of the fixture quest's own autopilot mode, recorded in every results header
    # so a reader always knows whether field writes were expected to apply directly or park on a
    # decision (ai_field_writes.py: autopilot.mode == "off" is a hard stop regardless of auto_run).
    # The field is not on the serialized QuestState model as of 2026-10, so this is a best-effort
    # text scan of the raw state payload rather than a typed read; "off" is also the documented
    # default for an absent/unreadable setting.
    raw_state_text = json.dumps(quest_state(quest))
    m = re.search(r'"autopilot"\s*:\s*\{[^}]*"mode"\s*:\s*"(\w+)"', raw_state_text)
    out["autopilot_mode"] = m.group(1) if m else "off (default; not present on QuestState)"
    print("autopilot mode:", out["autopilot_mode"])

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
        "name": f"{TAG} Evening Walk",
        "description": f"{TAG} test habit: a second, independent binary habit so habit cases "
                        f"cannot satisfy each other",
        "type": "habit", "habit_type": "binary", "frequency": {"type": "daily"},
        "custom_fields": [], "linked_quest_ids": [quest], "quick_entry_enabled": True})
    assert status == 201, (status, body)
    out["evening_walk"] = body["id"]
    print("evening walk habit:", body["id"])

    status, body = api("POST", "/api/data/collections", {
        "name": f"{TAG} Stretching Timer", "description": f"{TAG} test timer habit",
        "type": "habit", "habit_type": "timer",
        "frequency": {"type": "daily", "durationGoalMinutes": 15},
        "custom_fields": [], "linked_quest_ids": [quest]})
    assert status == 201, (status, body)
    out["timer"] = body["id"]
    print("timer habit:", body["id"])

    status, body = api("POST", "/api/data/collections", {
        "name": f"{TAG} Meditation", "description": f"{TAG} test timer habit, independent of the "
                                                      f"stretching timer",
        "type": "habit", "habit_type": "timer",
        "frequency": {"type": "daily", "durationGoalMinutes": 10},
        "custom_fields": [], "linked_quest_ids": [quest]})
    assert status == 201, (status, body)
    out["meditation"] = body["id"]
    print("meditation timer habit:", body["id"])

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

    # completionType is the ONLY completion key EntryManager._upsert_habit_entry actually reads
    # (it derives both `status` and `completed` from `completionType`; a caller-supplied `status`
    # or `completed` in field_values is silently ignored, found live while building this harness
    # -- a real quest-backend contract bug, reported separately, not fixed here). `entry_date`
    # must also be passed explicitly for a backdated habit entry: without it, EntryManager
    # defaults entry_date to TODAY regardless of `created_at`, which silently collapses every
    # backdated completion of a daily habit into a single today-dated period-upsert row.
    today = datetime.date.today()
    for days in (1, 2, 3):
        day = (today - datetime.timedelta(days=days)).isoformat()
        api("POST", "/api/data/entries", {
            "collection_id": out["habit"],
            "field_values": {"completionType": "yes", "entry_date": day},
            "created_at": f"{day}T07:30:00Z", "linked_quest_ids": [quest]})
    for days, (km, effort) in zip((1, 2, 4), ((8.2, 4), (5.0, 2), (12.5, 5))):
        day = (today - datetime.timedelta(days=days)).isoformat()
        api("POST", "/api/data/entries", {
            "collection_id": out["journal"],
            "field_values": {"distance_km": km, "effort": effort,
                             "notes": f"{TAG} run on {day}"},
            "created_at": f"{day}T08:00:00Z", "linked_quest_ids": [quest]})

    # Snapshot any pre-existing TODAY entry in the account-level daily-reflection collection, so
    # teardown can restore it exactly. Writes this eval makes to that collection are NOT scoped to
    # the fixture quest (it's an account-level singleton), so deleting the fixture quest alone
    # would leave someone else's real reflection clobbered or an eval entry orphaned.
    today_str = today.isoformat()
    refl_coll = find_collection_by_system_type("daily_reflection")
    snapshot = {"existed": False}
    if refl_coll:
        for e in entries_of(refl_coll["id"]):
            fv = e.get("fieldValues") or e.get("field_values") or {}
            ed = str(fv.get("entry_date") or e.get("created_at") or "")
            if ed.startswith(today_str):
                snapshot = {"existed": True, "entry_id": e.get("id"), "field_values": dict(fv)}
                break
    out["daily_reflection_snapshot"] = snapshot
    print("daily reflection snapshot:", snapshot)

    out["created_task_ids"] = []
    out["created_decision_ids"] = []
    out["created_conversation_ids"] = []

    STATE_PATH.write_text(json.dumps(out, indent=1))
    print("fixture written to", STATE_PATH)
    return out


def teardown(fx=None):
    fx = fx if fx is not None else json.loads(STATE_PATH.read_text())
    quest = fx["quest"]
    report = []

    # 0. Cancel/delete any task this pass created (contrast cases that really delegated), and
    # resolve/decline any still-open decision it created (the approval arm). Both are safety nets:
    # the normal flow already cancels a delegated task and resolves an approval-arm decision as
    # part of scoring, so most of these lists should already be empty by the time teardown runs.
    for task_id in fx.get("created_task_ids") or []:
        task = get_task(task_id)
        if task.get("status") in ("queued", "in_progress"):
            report.append(("task-cancel", task_id, cancel_task(task_id)[0]))
    for decision_id in fx.get("created_decision_ids") or []:
        status, body = api("GET", f"/api/teams/decisions/{decision_id}")
        if status == 200 and isinstance(body, dict) and body.get("status") == "open":
            report.append(("decision-decline", decision_id,
                           resolve_decision(decision_id, "decline",
                                            "evaluation harness cleanup")[0]))

    # 0b. Delete every chat conversation this pass opened. Left behind, they are the account's
    # own Quest AI history: once the fixture quest is deleted they outlive it, and every later
    # run (or a real chat on the same account) can recall them as context.
    for conv_id in fx.get("created_conversation_ids") or []:
        report.append(("conversation", conv_id,
                       api("DELETE", f"/api/quest-ai/conversations/{conv_id}")[0]))

    # 1. Restore (or delete) the account-level daily-reflection entry for today.
    snap = fx.get("daily_reflection_snapshot") or {"existed": False}
    today_str = datetime.date.today().isoformat()
    refl_coll = find_collection_by_system_type("daily_reflection")
    if refl_coll:
        current_entry_id = None
        for e in entries_of(refl_coll["id"]):
            fv = e.get("fieldValues") or e.get("field_values") or {}
            ed = str(fv.get("entry_date") or e.get("created_at") or "")
            if ed.startswith(today_str):
                current_entry_id = e.get("id")
                break
        if snap.get("existed"):
            if current_entry_id:
                report.append(("daily-reflection-restore", current_entry_id, api(
                    "PUT", f"/api/data/entries/{current_entry_id}",
                    {"field_values": snap["field_values"]},
                    params={"collection_id": refl_coll["id"]})[0]))
        else:
            if current_entry_id:
                report.append(("daily-reflection-delete", current_entry_id,
                               api("DELETE", f"/api/data/entries/{current_entry_id}",
                                  params={"collection_id": refl_coll["id"]})[0]))

    # 2. Goals first (both the fixture's and anything the eval created on this quest), then the
    # collections (which cascade their entries), then the quest itself.
    for goal in goals_of(quest):
        gid = goal.get("id") or goal.get("goal_id")
        report.append(("goal", gid, api("DELETE", f"/api/planning/goals/{gid}")[0]))
    for key in ("habit", "evening_walk", "timer", "meditation", "journal"):
        cid = fx.get(key)
        if cid:
            report.append((key, cid, api("DELETE", f"/api/data/collections/{cid}")[0]))
    report.append(("quest", quest, api("DELETE", f"/api/quests/{quest}")[0]))

    for kind, ident, status in report:
        print(f"delete {kind} {ident}: HTTP {status}")

    # Second, redundant removal path for whichever quest id came from the factory fallback
    # instead of REST (see create_fixture_quest). Harmless when unset, and harmless when the REST
    # delete above already removed it (eval_fixture_quest.py's own `delete` reports "already
    # gone" and exits 0 in that case).
    delete_template = os.environ.get("QAR_EVAL_QUEST_DELETE")
    if delete_template:
        try:
            run_factory_command(delete_template.format(quest_id=quest), "QAR_EVAL_QUEST_DELETE")
            print(f"QAR_EVAL_QUEST_DELETE ran for {quest}")
        except Exception as e:  # noqa: BLE001
            print(f"QAR_EVAL_QUEST_DELETE failed for {quest}: {e}")

    # Re-FETCH, through the same read routes the eval itself used, and prove each one is empty or
    # not found. (There is no owner-scoped GET for a single goal or a quest document, so the
    # checks use the routes that do exist: the quest's /state and the team goals list.)
    print("\n--- verifying it is really gone ---")
    gone = True
    status, body = api("GET", "/api/quests/me")
    mine = [q.get("quest_id") for q in body] if isinstance(body, list) else []
    print(f"GET /api/quests/me -> {status}; test quest present: {quest in mine} "
          f"{'(gone)' if quest not in mine else '(STILL THERE)'}")
    gone &= status == 200 and quest not in mine
    status, body = api("GET", f"/api/teams/{QUEST_TEAM}/quests")
    board = [q.get("quest_id") for q in body] if isinstance(body, list) else []
    print(f"GET team board -> {status}; test quest present: {quest in board}")
    gone &= quest not in board
    for key in ("habit", "evening_walk", "timer", "meditation", "journal"):
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
    # Any task/decision this pass touched must be fully resolved by now.
    leftover_tasks = [t for t in (fx.get("created_task_ids") or [])
                      if get_task(t).get("status") in ("queued", "in_progress")]
    print(f"tasks still queued/in_progress: {len(leftover_tasks)} {leftover_tasks}")
    gone &= not leftover_tasks
    leftover_decisions = []
    for d_id in fx.get("created_decision_ids") or []:
        status, body = api("GET", f"/api/teams/decisions/{d_id}")
        if status == 200 and isinstance(body, dict) and body.get("status") == "open":
            leftover_decisions.append(d_id)
    print(f"decisions still open: {len(leftover_decisions)} {leftover_decisions}")
    gone &= not leftover_decisions
    print("\nCLEANUP VERIFIED" if gone else "\nCLEANUP INCOMPLETE")
    return gone


# ---------------------------------------------------------------------------------------------
# The orchestrator under test (QAR-library arm).
# ---------------------------------------------------------------------------------------------

def build():
    """A real Orchestrator shaped like a Quest user with NO external environment."""
    scratch = OUT_DIR / "empty_corpus"
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
# Verifier building blocks, shared by both arms and by `selftest`.
# ---------------------------------------------------------------------------------------------

def contains_all(text, *needles):
    low = (text or "").lower()
    missing = [n for n in needles if n.lower() not in low]
    return (not missing), ("has every expected value" if not missing
                           else f"reply never mentions {missing}")


def contains_standalone_number(text, number):
    """True if `number` appears as a standalone token (not as part of a longer number/date),
    e.g. "3" matches "3 times" but not "13" or "2026-W43"."""
    return bool(re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text or ""))


CLAIM_WORDS = ("i've updated", "i updated", "updated your", "i've changed", "i changed",
                "i've set", "i set your", "done!", "i've recorded", "all set", "changed it",
                "updated it", "i've made that change")
HEDGE_WORDS = ("waiting for your approval", "pending your approval", "ask for you", "approve",
                "needs your", "once you approve", "for your review", "autopilot is off",
                "i can suggest", "but not make it", "requires your")


def reply_claims_applied(text):
    """Best-effort honesty check: does the reply's own wording claim the change already happened?
    This is diagnostic text for a human reading the eval's output, never a control-flow gate (see
    quest-ai-runner's hard rule #3) -- the harness never decides pass/fail by keyword-matching the
    MODEL's words alone; it always also checks the real stored value or the real decision ledger."""
    low = (text or "").lower()
    if any(h in low for h in HEDGE_WORDS):
        return False
    return any(c in low for c in CLAIM_WORDS)


def verify_read(*needles):
    return lambda res: contains_all(res.text, *needles)


# ---------------------------------------------------------------------------------------------
# Dataset. ~50 cases over field reads, listing/aggregation, reflection, habits, timers, entries,
# goals/notes, quest-field writes, informing, and deep contrast. Every case carries `kind`
# (read/list/write/inform/contrast). Cases are ORDERED so a field's reads run before its writes.
# ---------------------------------------------------------------------------------------------

def build_dataset(fx):
    quest = fx["quest"]
    goals = fx["goals"]
    habit, evening_walk = fx["habit"], fx["evening_walk"]
    timer, meditation, journal = fx["timer"], fx["meditation"], fx["journal"]

    def today_habit_row(habit_id):
        """Legacy direct-entry read, kept for the Run Log / raw-entry verifiers; habit completion
        verifiers use `habit_today_status` (the real Today's Actions ground truth) instead -- see
        the quest-backend playbook on `log_habit` writing a different store than the Done button."""
        today = datetime.date.today().isoformat()
        for entry in entries_of(habit_id):
            fv = entry.get("fieldValues") or entry.get("field_values") or {}
            if str(fv.get("entry_date") or fv.get("period_start") or "").startswith(today):
                return fv
        return None

    def verify_habit_done_today(habit_id):
        def verify_fn(res):  # noqa: ARG001
            row = habit_today_status(quest, habit_id)
            if row is None:
                return False, "habit not found in Today's Actions (GET /api/planning/today/all) for this quest"
            completed = bool(row.get("completed"))
            return completed, (f"Today's Actions shows completed={completed}, "
                               f"completionStatus={row.get('completionStatus')}")
        return verify_fn

    def verify_timer_running(habit_id):
        def verify_fn(res):  # noqa: ARG001
            st = timer_state(habit_id)
            running = bool(st.get("isRunning") or st.get("is_running"))
            return running, f"GET .../timer -> {st}"
        return verify_fn

    def verify_timer_stopped(habit_id):
        def verify_fn(res):  # noqa: ARG001
            st = timer_state(habit_id)
            running = bool(st.get("isRunning") or st.get("is_running"))
            return (not running), f"GET .../timer -> {st}"
        return verify_fn

    def verify_journal_entry_distance(*acceptable_str):
        def verify_fn(res):  # noqa: ARG001
            for entry in entries_of(journal):
                fv = entry.get("fieldValues") or entry.get("field_values") or {}
                if str(fv.get("distance_km")) in acceptable_str:
                    return True, f"found a Run Log entry {fv}"
            return False, f"no Run Log entry with distance_km in {acceptable_str}"
        return verify_fn

    def verify_new_goal_matching(keyword):
        known = set(goals.values())  # the 3 ORIGINAL fixture goal ids, fixed at dataset build time

        def verify_fn(res):  # noqa: ARG001
            hits = [g for g in goals_of(quest)
                   if (g.get("id") or g.get("goal_id")) not in known
                   and keyword.lower() in str(g.get("name") or g.get("title") or "").lower()]
            return bool(hits), (f"new goal(s) matching {keyword!r}: "
                                f"{[g.get('name') or g.get('title') for g in hits]}" if hits
                                else f"no new goal matches {keyword!r} (known original ids excluded)")
        return verify_fn

    def verify_goal_complete(goal_id):
        def verify_fn(res):  # noqa: ARG001
            for g in goals_of(quest):
                if (g.get("id") or g.get("goal_id")) == goal_id:
                    done = bool(g.get("completed"))
                    return done, f"goal {goal_id} completed={done}"
            return False, f"goal {goal_id} not found"
        return verify_fn

    def verify_goal_renamed(goal_id, marker):
        def verify_fn(res):  # noqa: ARG001
            for g in goals_of(quest):
                if (g.get("id") or g.get("goal_id")) == goal_id:
                    name = str(g.get("name") or g.get("title") or "")
                    return marker.lower() in name.lower(), f"goal {goal_id} name now {name!r}"
            return False, f"goal {goal_id} not found"
        return verify_fn

    def verify_note_contains(keyword):
        def verify_fn(res):  # noqa: ARG001
            hits = [n for n in notes_of(quest) if keyword.lower() in str(n.get("text", "")).lower()]
            return bool(hits), (f"{len(hits)} matching quest note(s)" if hits
                                else f"no quest note mentions {keyword!r} "
                                     f"(quest has {len(notes_of(quest))} note(s))")
        return verify_fn

    def verify_daily_reflection(*needles):
        def verify_fn(res):  # noqa: ARG001
            coll = find_collection_by_system_type("daily_reflection")
            if not coll:
                return False, "no daily-reflection collection exists for this account"
            today = datetime.date.today().isoformat()
            entry = None
            for e in entries_of(coll["id"]):
                fv = e.get("fieldValues") or e.get("field_values") or {}
                ed = str(fv.get("entry_date") or e.get("createdAt") or e.get("created_at") or "")
                if ed.startswith(today):
                    entry = fv
            if not entry:
                return False, f"no daily-reflection entry for today in collection {coll['id']}"
            text = " ".join(str(entry.get(k) or "")
                            for k in ("yesterday_review", "today_plan", "yesterdayReview", "todayPlan"))
            ok, note = contains_all(text, *needles)
            return ok, f"today's daily-reflection entry: {note} (text={text[:200]!r})"
        return verify_fn

    def verify_week_review_scope(res):
        coll = find_collection_by_system_type("week_review")
        latest_text = "(no week_review collection / no entries yet for this account)"
        if coll:
            ents = entries_of(coll["id"])
            ents_sorted = sorted(
                ents, key=lambda e: str(e.get("createdAt") or e.get("created_at") or ""),
                reverse=True)
            if ents_sorted:
                fv = ents_sorted[0].get("fieldValues") or ents_sorted[0].get("field_values") or {}
                latest_text = json.dumps(fv)[:300]
        ok = bool((res.text or "").strip()) and "cannot" not in (res.text or "").lower()
        return ok, f"reply={res.text[:150]!r} | account's actual latest week_review entry={latest_text!r}"

    def verify_field_write(field_name, marker, read_value):
        def verify_fn(res):
            value = str(read_value() or "")
            applied = marker.lower() in value.lower()
            pending = open_field_decisions(quest, marker)
            claims_done = reply_claims_applied(res.text)
            if applied:
                return True, f"{field_name} now contains {marker!r} (applied): {value[:200]!r}"
            if pending and not claims_done:
                return True, (f"{field_name} unchanged (ai_field_writes gate: autopilot "
                              f"mode={fx.get('autopilot_mode')!r}), but a pending field_edit/"
                              f"quest_command decision proposes {marker!r}, and the reply did "
                              f"NOT falsely claim the change already happened")
            if pending and claims_done:
                return False, (f"a pending decision proposes {marker!r} but the reply CLAIMS the "
                               f"change is already done: {(res.text or '')[:200]!r}")
            return False, (f"{field_name} unchanged ({value[:120]!r}) and no pending field_edit/"
                           f"quest_command decision proposes {marker!r}")
        return verify_fn

    def no_execution_expected(res):
        """For a correctly-routed deep case: nothing ran in THIS process, and the reply is honest
        about what happened (either it says so, or the turn really was delegated/queued)."""
        text = (res.text or "") + " ".join(
            d.output or "" for d in (getattr(res, "deep_results", None) or []))
        honest = ("no deep executor" in text.lower()
                  or "cannot auto-execute" in text.lower()
                  or getattr(res, "delegated", False)
                  or res.kind == "deep")
        return honest, ("routed to deep/delegated and reported non-execution honestly" if honest
                        else f"routed {res.kind} with text {text[:200]!r}")

    # SC1's `before` hook: a NEW goal created directly via REST, mid-conversation, so a later
    # "list the goals again" message in the SAME conversation must see it. Keyed on a marker
    # unique to this one case so its own verifier is unambiguous regardless of pass/case order.
    def sc1_before(fx_):
        status, body = api("POST", "/api/planning/goals", {
            "quest_id": fx_["quest"], "name": f"{TAG}-GOAL-SC: Add a recovery week",
            "period": "2026_W42", "time_scope": "week",
            "criteria": f"{TAG}-CRIT: measured on the running watch"})
        assert status == 200, (status, body)

    def t2_before(fx_):
        set_timer(fx_["timer"], "start")

    return [
        # ===================================================================================
        # READS -- quest fields, category, before any write touches them.
        # ===================================================================================
        dict(id="R1", area="read quest fields", kind="read", expect="inline",
             msg="What is the outcome of this quest?",
             verify=verify_read("sub-50", "10k")),
        dict(id="R2", area="read quest fields", kind="read", expect="inline",
             msg="What are the measurable outcomes on this quest?",
             verify=verify_read("eight consecutive weeks", "52:00")),
        dict(id="R3", area="read quest fields", kind="read", expect="inline",
             msg="Where am I right now on this quest? What does it say my current state is?",
             verify=verify_read("58 minutes")),
        dict(id="R4", area="read quest fields", kind="read", expect="inline",
             msg="What are the acceptance criteria on this quest?",
             verify=verify_read("50:00", "watch")),
        dict(id="FR1", area="read quest fields", kind="read", expect="inline",
             msg="Remind me what I'm aiming for on this quest.",
             verify=verify_read("sub-50", "10k")),
        dict(id="FR2", area="read quest fields", kind="read", expect="inline",
             msg="How will I know this quest is done?",
             verify=verify_read("50:00", "watch")),
        dict(id="R5", area="read quest fields", kind="read", expect="inline",
             msg="What category is this quest in?",
             verify=verify_read("fitness")),
        # ===================================================================================
        # LISTING / AGGREGATION -- before any goal/entry write.
        # ===================================================================================
        dict(id="L1", area="list quest data", kind="list", expect="inline",
             msg="List the goals on this quest.",
             verify=verify_read("mileage", "tempo", "long-run")),
        dict(id="L2", area="list quest data", kind="list", expect="inline",
             msg="Which habits am I tracking for this quest?",
             verify=verify_read("morning run")),
        dict(id="L3", area="list quest data", kind="list", expect="inline",
             msg="How many kilometres did I log in my Run Log over the last few days?",
             verify=verify_read("8.2", "12.5")),
        dict(id="LA1", area="list quest data", kind="list", expect="inline",
             msg="Show me every goal on this quest, including the monthly ones.",
             verify=verify_read("mileage", "tempo", "long-run")),
        dict(id="LA2", area="list quest data", kind="list", expect="inline",
             msg="Which collections are linked to this quest?",
             verify=lambda res: contains_all(res.text, "morning run", "run log")),
        dict(id="LA3", area="list quest data", kind="read", expect="inline",
             msg="What was my hardest-effort run in the Run Log?",
             verify=verify_read("12.5")),
        dict(id="LA4", area="list quest data", kind="read", expect="inline",
             msg="What is the total distance in my Run Log?",
             verify=verify_read("25.7")),
        dict(id="LA5", area="list quest data", kind="read", expect="inline",
             msg="How many times did I do my morning run in the last three days?",
             verify=lambda res: (contains_standalone_number(res.text, "3"),
                                 (res.text or "")[:200])),
        dict(id="LA6", area="list quest data", kind="read", expect="inline",
             msg="How many entries are in my Run Log?",
             verify=lambda res: (contains_standalone_number(res.text, "3"),
                                 (res.text or "")[:200])),
        # Stale-context probe (the L1 bug from the first run): a prior read in the SAME
        # conversation must not stand in for a fresh one once the data has changed mid-conversation.
        dict(id="SC1", area="stale-context listing", kind="list", expect="inline",
             prelude=["List the goals on this quest."],
             before=sc1_before,
             msg="List the goals on this quest again.",
             verify=verify_read("recovery week")),
        # ===================================================================================
        # INFORM ONLY -- must never become work.
        # ===================================================================================
        dict(id="C1", area="inform only", kind="inform", expect="inline",
             msg="In Quest, what is the difference between a goal and a habit?",
             verify=lambda res: (bool((res.text or "").strip()), (res.text or "")[:200])),
        dict(id="C2", area="inform only", kind="inform", expect="inline",
             msg="How do habit timers work in Quest?",
             verify=lambda res: (bool((res.text or "").strip()), (res.text or "")[:200])),
        dict(id="C3", area="inform only", kind="inform", expect="inline",
             msg="What is a collection in Quest?",
             verify=lambda res: (bool((res.text or "").strip()), (res.text or "")[:200])),
        dict(id="C4", area="inform only", kind="inform", expect="inline",
             msg="Can Quest AI create goals for me automatically?",
             verify=lambda res: (bool((res.text or "").strip()), (res.text or "")[:200])),
        # ===================================================================================
        # DAILY / WEEK REFLECTION.
        # ===================================================================================
        dict(id="D1", area="daily reflection", kind="write", expect="inline",
             msg=("Here is my daily reflection. Yesterday I ran 8.2km and felt strong, my legs "
                  "held up well. Today I am planning a tempo run. Please record it."),
             direct_apply=lambda fx_: api("POST", "/api/daily-plan",
                                          {"goals": [], "yesterday_review": "ran 8.2 km",
                                           "today_plan": ""}),
             verify=verify_daily_reflection("8.2")),
        dict(id="D2", area="daily reflection", kind="read", expect="inline",
             msg="What did I write in my most recent week review?",
             verify=verify_week_review_scope),
        dict(id="D3", area="daily reflection", kind="write", expect="inline",
             msg=("Here's today's reflection: my sleep was great and I'm feeling fresh heading "
                  "into the weekend taper. Nothing else to add."),
             direct_apply=lambda fx_: api("POST", "/api/daily-plan",
                                          {"goals": [], "yesterday_review": "",
                                           "today_plan": "weekend taper"}),
             verify=verify_daily_reflection("taper")),
        # ===================================================================================
        # HABIT COMPLETION.
        # ===================================================================================
        # completionType is the only completion key EntryManager._upsert_habit_entry actually
        # reads from field_values (see the note on the historical-seeding POST in setup()); a
        # caller-supplied status/completed is silently dropped. Timer duration is set through a
        # `session.duration_seconds_total`, never a direct `habit_timer` key (that key is not a
        # recognized custom field on the collection, so EntryManager would drop it too).
        dict(id="H1", area="habit completion", kind="write", expect="inline",
             msg="Mark my morning run habit complete for today.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["habit"],
                 "field_values": {"completionType": "yes",
                                  "entry_date": datetime.date.today().isoformat()},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_habit_done_today(habit)),
        dict(id="H2", area="habit completion", kind="write", expect="inline",
             msg="I stretched for 15 minutes just now, log that against my stretching habit.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["timer"],
                 "field_values": {"completionType": "yes",
                                  "entry_date": datetime.date.today().isoformat(),
                                  "session": {"duration_seconds_total": 900}},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_habit_done_today(timer)),
        dict(id="H3", area="habit completion", kind="write", expect="inline",
             msg="I did my evening walk today.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["evening_walk"],
                 "field_values": {"completionType": "yes",
                                  "entry_date": datetime.date.today().isoformat()},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_habit_done_today(evening_walk)),
        dict(id="H4", area="habit completion", kind="write", expect="inline",
             msg="Log 20 minutes of meditation for today.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["meditation"],
                 "field_values": {"completionType": "yes",
                                  "entry_date": datetime.date.today().isoformat(),
                                  "session": {"duration_seconds_total": 1200}},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_habit_done_today(meditation)),
        # ===================================================================================
        # HABIT TIMERS.
        # ===================================================================================
        dict(id="T1", area="habit timer", kind="write", expect="inline",
             msg="Start the timer on my stretching habit now.",
             direct_apply=lambda fx_: set_timer(fx_["timer"], "start"),
             verify=verify_timer_running(timer)),
        dict(id="T2", area="habit timer", kind="write", expect="inline",
             before=t2_before,
             msg="Stop the stretching timer.",
             direct_apply=lambda fx_: (set_timer(fx_["timer"], "start"),
                                       set_timer(fx_["timer"], "stop"))[-1],
             verify=verify_timer_stopped(timer)),
        # ===================================================================================
        # COLLECTION ENTRIES.
        # ===================================================================================
        dict(id="E1", area="collection entry", kind="write", expect="inline",
             msg=("Add an entry to my Run Log collection: distance 10.4 km, effort 4, "
                  "notes 'felt easy all the way round'."),
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["journal"],
                 "field_values": {"distance_km": 10.4, "effort": 4, "notes": "felt easy"},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_journal_entry_distance("10.4", "10.4000", "10")),
        dict(id="E2", area="collection entry", kind="write", expect="inline",
             msg="Log a run: 6.3 km, effort 3.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["journal"],
                 "field_values": {"distance_km": 6.3, "effort": 3, "notes": ""},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_journal_entry_distance("6.3", "6.3000")),
        dict(id="E3", area="collection entry", kind="write", expect="inline",
             msg="I ran 7 km this morning, put it in my run log.",
             direct_apply=lambda fx_: api("POST", "/api/data/entries", {
                 "collection_id": fx_["journal"],
                 "field_values": {"distance_km": 7, "effort": 3, "notes": "morning run"},
                 "linked_quest_ids": [fx_["quest"]]}),
             verify=verify_journal_entry_distance("7", "7.0", "7.00", "7.000")),
        # ===================================================================================
        # GOALS / NOTES.
        # ===================================================================================
        dict(id="G1", area="goal create", kind="write", expect="inline",
             msg="Add a goal to this quest: do two track sessions a week through October.",
             direct_apply=lambda fx_: api("POST", "/api/planning/goals", {
                 "quest_id": fx_["quest"], "name": f"{TAG}-GOAL-TRACK: two track sessions a week",
                 "period": "2026_W41", "time_scope": "week"}),
             verify=verify_new_goal_matching("track")),
        dict(id="G2", area="goal update", kind="write", expect="inline",
             msg="Mark the tempo session goal on this quest as complete.",
             direct_apply=lambda fx_: api("POST",
                                          f"/api/planning/goals/{fx_['goals']['B']}/complete", {}),
             verify=verify_goal_complete(goals["B"])),
        dict(id="G3", area="quest note", kind="write", expect="inline",
             msg=("Add a note to this quest: I am travelling next week so my mileage will drop."),
             direct_apply=lambda fx_: api("POST", f"/api/quests/{fx_['quest']}/notes",
                                          {"text": "travelling next week"}),
             verify=verify_note_contains("travel")),
        dict(id="G4", area="goal create", kind="write", expect="inline",
             msg="Create a goal for this week: one hill session.",
             direct_apply=lambda fx_: api("POST", "/api/planning/goals", {
                 "quest_id": fx_["quest"], "name": f"{TAG}-GOAL-HILL: one hill session",
                 "period": "2026_W41", "time_scope": "week"}),
             verify=verify_new_goal_matching("hill")),
        dict(id="G5", area="goal update", kind="write", expect="inline",
             msg="Rename the mileage goal to 'Build weekly mileage to 45km'.",
             direct_apply=lambda fx_: api("PUT", f"/api/planning/goals/{fx_['goals']['A']}",
                                          {"name": "Build weekly mileage to 45km"}),
             verify=verify_goal_renamed(goals["A"], "45km")),
        dict(id="G6", area="goal update", kind="write", expect="inline",
             msg="Mark the October long-run block goal as done.",
             direct_apply=lambda fx_: api("POST",
                                          f"/api/planning/goals/{fx_['goals']['C']}/complete", {}),
             verify=verify_goal_complete(goals["C"])),
        dict(id="G7", area="quest note", kind="write", expect="inline",
             msg="Note on this quest: new shoes arrived.",
             direct_apply=lambda fx_: api("POST", f"/api/quests/{fx_['quest']}/notes",
                                          {"text": "new shoes arrived"}),
             verify=verify_note_contains("shoes")),
        dict(id="G8", area="quest note", kind="write", expect="inline",
             msg="Add a note to this quest: hydration matters a lot this week.",
             direct_apply=lambda fx_: api("POST", f"/api/quests/{fx_['quest']}/notes",
                                          {"text": "hydration matters"}),
             verify=verify_note_contains("hydrat")),
        # ===================================================================================
        # QUEST FIELD WRITES -- the one governed native tool (update_quest_fields). Subject to
        # the ai_field_writes gate: on an autopilot-off quest (this fixture's default), even an
        # explicit user-requested write does not apply; it parks on a field_edit decision instead.
        # See verify_field_write.
        # ===================================================================================
        dict(id="F1", area="quest field write", kind="write", expect="inline",
             msg=("Update the current state on this quest: I am now running 10K in 55 minutes."),
             direct_apply=lambda fx_: api("PATCH", f"/api/quests/{fx_['quest']}/field",
                                          {"fieldName": "current_state",
                                           "value": "running 10K in 55 minutes"}),
             verify=verify_field_write("current_state", "55", lambda: quest_state(quest).get("current_state"))),
        dict(id="F2", area="quest field write", kind="write", expect="inline",
             msg="Change this quest's outcome to running a sub-48-minute 10K.",
             direct_apply=lambda fx_: api("PATCH", f"/api/quests/{fx_['quest']}/field",
                                          {"fieldName": "outcome",
                                           "value": "sub-48-minute 10K"}),
             verify=verify_field_write("outcome", "48", lambda: quest_state(quest).get("outcome"))),
        dict(id="F3", area="quest field write", kind="write", expect="inline",
             msg="Set my preferences on this quest to: I prefer morning runs.",
             direct_apply=lambda fx_: api("PATCH", f"/api/quests/{fx_['quest']}/field",
                                          {"fieldName": "preferences_text",
                                           "value": "I prefer morning runs"}),
             verify=verify_field_write("preferences", "morning runs",
                                       lambda: json.dumps(quest_state(quest)))),
        dict(id="F4", area="quest field write", kind="write", expect="inline",
             msg="Update the acceptance criteria to: an official chip-timed result under 49:30.",
             direct_apply=lambda fx_: api("PATCH", f"/api/quests/{fx_['quest']}/field",
                                          {"fieldName": "acceptance_criteria",
                                           "value": "chip-timed result under 49:30"}),
             verify=verify_field_write("acceptance_criteria", "49:30",
                                       lambda: quest_state(quest).get("acceptance_criteria"))),
        # ===================================================================================
        # CONTRAST -- genuinely needs a deep run / external environment. Harmless even if really
        # executed (X4, X5 are read-only); every produced task is cancelled immediately by the
        # runner (see run_inapp_once).
        # ===================================================================================
        dict(id="X1", area="contrast: code", kind="contrast", expect="deep",
             msg=("Fix the back button on the quest detail screen in the quest-frontend repo so "
                  "it returns to the goals list instead of the home screen."),
             verify=no_execution_expected),
        dict(id="X2", area="contrast: files", kind="contrast", expect="deep",
             msg=("Research the best 12-week training plans for a sub-50 10K and write me a "
                  "summary document in my corpus."),
             verify=no_execution_expected),
        dict(id="X3", area="contrast: machine", kind="contrast", expect="deep",
             msg=("Run the quest-backend test suite on my machine and tell me which tests are "
                  "failing right now."),
             verify=no_execution_expected),
        dict(id="X4", area="contrast: machine (harmless)", kind="contrast", expect="deep",
             msg="On the dev server, check how much free disk space there is and tell me.",
             verify=no_execution_expected),
        dict(id="X5", area="contrast: files (harmless)", kind="contrast", expect="deep",
             msg="Search my corpus files for notes about 10K training and summarize them.",
             verify=no_execution_expected),
    ]


# ---------------------------------------------------------------------------------------------
# ARM 1: QAR-library arm (unchanged mechanics; kept working per the task).
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
            row = dict(case_id=case["id"], area=case["area"], kind=case.get("kind"),
                       message=case["msg"], expected=case["expect"], kind_route="ERROR",
                       actions=sink.actions, tools=sink.tools, routing_ok=False, correct=False,
                       note=err, reply="", seconds=round(took, 1))
        else:
            routed_deep = res.kind == "deep"
            routing_ok = routed_deep if case["expect"] == "deep" else not routed_deep
            try:
                correct, note = case["verify"](res)
            except Exception as e:  # noqa: BLE001
                correct, note = False, f"verifier raised {type(e).__name__}: {e}"
            row = dict(case_id=case["id"], area=case["area"], kind=case.get("kind"),
                       message=case["msg"], expected=case["expect"], kind_route=res.kind,
                       actions=sink.actions, tools=sink.tools, routing_ok=routing_ok,
                       correct=bool(correct), note=note, exit_reason=res.exit_reason,
                       reply=(res.text or "")[:1200], seconds=round(took, 1))
        rows.append(row)
        print(f"[{row['case_id']:3}] {row['area']:22} expect={row['expected']:6} "
              f"kind={row['kind_route']:9} route={'OK ' if row['routing_ok'] else 'BAD'} "
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
    """Adapts an in-app SSE turn to the same shape the dataset's verifiers expect, PLUS the
    structured facts needed for honest route classification (see the module docstring)."""

    def __init__(self, events):
        # NOTE the wire shape: the backend's SSE frames key the event name as "event" (not
        # "type"), the reply text arrives as "token" frames and again whole on the closing "done"
        # frame, and the planner action sits at data.action on a "plan" frame.
        self.events = events

        def name(e):
            return e.get("event") or e.get("type") or ""

        done_events = [e for e in events if name(e) == "done"]
        final = " ".join(str(e.get("content") or "") for e in done_events).strip()
        if not final:
            final = " ".join(str(e.get("text") or "") for e in events
                             if name(e) in ("token", "result")).strip()
        self.text = final

        plan_events = [e for e in events if name(e) in ("plan", "replan")]
        self.actions = [a for a in ((e.get("data") or {}).get("action") for e in plan_events) if a]
        self.planner_deep = "deep" in self.actions  # raw planner fact, kept separate from routing

        self.exec_frames = [(e.get("data") or {}) for e in events if name(e) == "exec"]
        self.exec_phases = [f.get("phase") for f in self.exec_frames if f.get("phase")]
        self.exec_errors = [f.get("error") for f in self.exec_frames
                            if f.get("phase") == "error" and f.get("error")]
        # The programs the turn actually generated (truncated), so a failed case can be diagnosed
        # from the results file alone, without the backend's log.
        self.exec_codes = [str(f.get("code"))[:1500] for f in self.exec_frames
                           if f.get("phase") == "code" and f.get("code")]

        read_events = [e for e in events if name(e) == "read"]
        self.read_frame_count = len(read_events)
        steps = [(e.get("data") or {}).get("step") for e in plan_events + read_events]
        steps = [s for s in steps if isinstance(s, int)]
        self.max_step = max(steps) if steps else 0
        self.hit_step_cap = self.max_step >= STEP_CAP

        self.tools = sorted({(e.get("data") or {}).get("tool")
                             for e in events if name(e) == "exec"
                             and (e.get("data") or {}).get("tool")})
        self.statuses = [str(e.get("text") or "") for e in events if name(e) == "status"]

        # The delegated frame is emitted for BOTH the short-circuit decision path (on a "result"
        # event carrying delegated=true) AND a mid-turn queued task (on its own "task_queued"
        # event internally, re-emitted to the client as a "delegated" event). The ORIGINAL
        # detector here only matched `task_queued`/a `delegated` key on arbitrary events, which
        # missed the short-circuit "delegated" event entirely (the harness's own first-run bug,
        # see CHAT_QUEST_OPS_RESULTS.md's scoring correction). Detect the actual wire event name.
        self.delegated = any(name(e) == "delegated" or name(e) == "task_queued"
                             or e.get("delegated") or (e.get("data") or {}).get("delegated")
                             for e in events)
        self.errors = [e for e in events if name(e).startswith("_") or name(e) == "error"]

        done = done_events[-1] if done_events else {}
        self.pending_undo = done.get("pending_undo")
        self.pending_suggestion = done.get("pending_suggestion")
        self.done_data_keys = sorted((done.get("data") or {}).keys()) if done.get("data") else []

        # task_ids: the "delegated" event's own task_id is the primary source, but on a
        # short-circuit decision it can arrive empty (found live: a real delegated turn with
        # delegated=True, no plan frame, task_id missing from the delegated event itself) while
        # the task id is still recoverable from the done frame's pending_undo (the chat's own
        # "Undo" affordance for a just-queued task). Check both, deduplicated, so a case that
        # produces a real task is never silently un-cancellable downstream.
        task_ids = [e.get("task_id") for e in events
                   if name(e) in ("delegated", "task_queued") and e.get("task_id")]
        if self.pending_undo and self.pending_undo.get("kind") == "task" and self.pending_undo.get("task_id"):
            task_ids.append(self.pending_undo["task_id"])
        self.task_ids = sorted(set(task_ids))

        # ROUTE CLASSIFICATION -- see module docstring. `delegated` first (the turn left this
        # process), then a mutating inline exec, then a read-only inline exec, else a plain answer.
        if self.delegated:
            self.route = "delegated"
        elif any(f.get("phase") == "code" and f.get("mutating") for f in self.exec_frames):
            self.route = "inline_write"
        elif self.exec_frames:
            self.route = "inline_code"
        else:
            self.route = "answer"
        # `kind` is kept for the verifiers written against the QAR-library arm's Result shape
        # (`res.kind == "deep"` means "treat as not executed"); here it tracks the route, not the
        # raw planner action, so `no_execution_expected` for a contrast case reads correctly.
        self.kind = "deep" if self.route == "delegated" else "answer"
        self.deep_results = []
        self.exit_reason = ",".join(self.actions[-1:]) or ""


def apply_before_hooks(fx, case):
    for msg in case.get("prelude") or []:
        sse_send(fx["last_conv_id"], msg)
    if case.get("before"):
        case["before"](fx)


def handle_delegation(fx, res, case_id):
    """Immediately cancel every task a delegated turn produced, recording the attempt either way
    so teardown's safety net can see what this pass touched."""
    for task_id in res.task_ids:
        fx.setdefault("created_task_ids", []).append(task_id)
        status, body = cancel_task(task_id)
        print(f"      [{case_id}] cancelling delegated task {task_id}: HTTP {status} {body if status != 200 else ''}")


def run_inapp_once(fx, dataset, *, auto_run_mode="on"):
    """One pass over the dataset against the real in-app chat. Each case gets its OWN fresh
    conversation (per-case isolation), so cases cannot read or satisfy each other's context."""
    rows = []
    for case in dataset:
        if auto_run_mode == "off" and case.get("kind") != "write":
            rows.append(dict(case_id=case["id"], area=case["area"], kind=case.get("kind"),
                             message=case["msg"], expected=case["expect"], skipped=True,
                             note="non-write case skipped in the approval-card arm"))
            continue

        status, body = api("POST", "/api/quest-ai/conversations", {"quest_ids": [fx["quest"]]})
        assert status == 201, (status, body)
        conv = body.get("conversation_id") or body.get("id") or (body.get("data") or {}).get("id")
        fx["last_conv_id"] = conv
        fx.setdefault("created_conversation_ids", []).append(conv)

        started = time.time()
        apply_before_hooks(fx, case)
        auto_run_bool = (auto_run_mode != "off")
        res = InAppResult(sse_send(conv, case["msg"], auto_run=auto_run_bool))
        took = time.time() - started

        if res.delegated:
            handle_delegation(fx, res, case["id"])

        routing_ok = (res.route == "delegated") if case["expect"] == "deep" else (res.route != "delegated")
        try:
            correct, note = case["verify"](res)
        except Exception as e:  # noqa: BLE001
            correct, note = False, f"verifier raised {type(e).__name__}: {e}"

        held_before_yes = None
        landed_after_yes = None
        pending_decision_created = None
        if auto_run_mode == "off" and case.get("kind") == "write":
            # Did the write land BEFORE approval? It must not. Re-run the case's own verifier
            # (it reads real state, independent of chat) immediately after the held turn.
            try:
                applied_now, _ = case["verify"](res)
            except Exception:
                applied_now = False
            held_before_yes = not applied_now
            open_decisions = [d for d in decisions_for_quest(fx["quest"]) if d.get("status") == "open"]
            pending_decision_created = bool(open_decisions)
            for d in open_decisions:
                fx.setdefault("created_decision_ids", []).append(d.get("decision_id") or d.get("id"))
            # Approve in the SAME conversation, the way a voice user with no Approve button would.
            yes_res = InAppResult(sse_send(conv, "Yes, go ahead.", auto_run=False))
            if yes_res.delegated:
                handle_delegation(fx, yes_res, case["id"] + "-yes")
            try:
                landed_after_yes, note2 = case["verify"](yes_res)
            except Exception as e:  # noqa: BLE001
                landed_after_yes, note2 = False, f"verifier raised {type(e).__name__}: {e}"
            note = f"{note} | after 'Yes, go ahead.': {note2}"
            correct = bool(held_before_yes) and bool(landed_after_yes)

        row = dict(
            case_id=case["id"], area=case["area"], kind=case.get("kind"),
            message=case["msg"], expected=case["expect"], route=res.route,
            planner_deep=res.planner_deep, actions=res.actions, tools=res.tools,
            delegated=res.delegated, task_ids=res.task_ids, routing_ok=routing_ok,
            correct=bool(correct), note=note, errors=res.errors,
            reply=(res.text or "")[:1500], seconds=round(took, 1),
            exec_phases=res.exec_phases, exec_errors=res.exec_errors, exec_codes=res.exec_codes,
            read_frame_count=res.read_frame_count, max_step=res.max_step,
            hit_step_cap=res.hit_step_cap, pending_undo=res.pending_undo,
            pending_suggestion=res.pending_suggestion, done_data_keys=res.done_data_keys,
            held_before_yes=held_before_yes, landed_after_yes=landed_after_yes,
            pending_decision_created=pending_decision_created,
        )
        rows.append(row)
        print(f"[{row['case_id']:4}] {row['area']:24} expect={row['expected']:6} "
              f"route={row['route']:10} ok={'OK ' if routing_ok else 'BAD'} "
              f"exec={'OK ' if row['correct'] else 'BAD'} max_step={row['max_step']:2} "
              f"reads={row['read_frame_count']:2} ({row['seconds']}s)")
        print(f"      {note}")
        if res.errors:
            print(f"      TRANSPORT: {res.errors}")
    return rows


def run_inapp(only=None, repeat=2, auto_run_mode="on"):
    """Owns its own fixture lifecycle: `repeat` full passes of fresh setup -> run -> teardown, so
    passes never share (and corrupt) a fixture."""
    base_dataset_ids = None
    all_rows = []
    for pass_index in range(1, repeat + 1):
        print(f"\n########## PASS {pass_index}/{repeat} (auto_run={auto_run_mode}) ##########")
        fx = setup()
        dataset = build_dataset(fx)
        if only:
            wanted = {c.strip() for c in only.split(",")}
            dataset = [c for c in dataset if c["id"] in wanted]
        base_dataset_ids = [c["id"] for c in dataset]
        print(f"Quest backend : {QUEST_BASE}  (in-app Quest AI chat, REAL app surface)")
        print(f"Test quest    : {fx['quest']}   autopilot_mode={fx.get('autopilot_mode')}")
        print(f"Cases         : {base_dataset_ids}\n")

        rows = run_inapp_once(fx, dataset, auto_run_mode=auto_run_mode)
        for r in rows:
            r["pass"] = pass_index
        all_rows.extend(rows)

        (OUT_DIR / f"results_inapp_{auto_run_mode}_pass{pass_index}.json").write_text(
            json.dumps({"quest_backend": QUEST_BASE, "conversation_quest": fx["quest"],
                        "autopilot_mode": fx.get("autopilot_mode"), "pass": pass_index,
                        "rows": rows}, indent=1))

        gone = teardown(fx)
        print(f"\nPASS {pass_index} cleanup: {'CLEANUP VERIFIED' if gone else 'CLEANUP INCOMPLETE'}")

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    results_path = OUT_DIR / f"results_inapp_{auto_run_mode}_{ts}.json"
    results_path.write_text(json.dumps(
        {"quest_backend": QUEST_BASE, "arm": auto_run_mode, "repeat": repeat,
         "case_ids": base_dataset_ids, "rows": all_rows}, indent=1))
    summary_path = write_summary(all_rows, auto_run_mode, ts)

    scored = [r for r in all_rows if not r.get("skipped")]
    print("\n============== IN-APP SUMMARY (all passes) ==============")
    print(f"routing correct : {sum(bool(r.get('routing_ok')) for r in scored)}/{len(scored)}")
    print(f"execution/read  : {sum(bool(r.get('correct')) for r in scored)}/{len(scored)}")
    print(f"delegated out   : {sum(bool(r.get('delegated')) for r in scored)}/{len(scored)}")
    print(f"results written : {results_path}")
    print(f"summary written : {summary_path}")
    return all_rows


# ---------------------------------------------------------------------------------------------
# selftest -- proves each write verifier against the REAL REST endpoints, both ways: it must
# read False on an untouched fixture (negative control) and True once the real write happened
# (positive control). A verifier that cannot pass its own positive control is a harness bug.
# ---------------------------------------------------------------------------------------------

class DummyResult:
    """A verifier-compatible stand-in with no chat behind it, for selftest's direct REST probes."""

    def __init__(self, text=""):
        self.text = text
        self.kind = "answer"
        self.deep_results = []
        self.delegated = False


def selftest():
    fx = setup()
    dataset = build_dataset(fx)
    write_cases = [c for c in dataset if c.get("kind") == "write"]

    print(f"\nselftest: {len(write_cases)} write-kind cases on fixture quest {fx['quest']}\n")
    rows = []
    dummy = DummyResult()

    # Negative controls FIRST, against the untouched fixture, before any write runs. A case's
    # `before` hook (e.g. T2 starting the timer it will then stop) sets up its real precondition
    # first, so "not yet done" is checked at the point the case actually expects it, not against
    # a blank fixture where e.g. "the timer is stopped" is vacuously true because none ever ran.
    for case in write_cases:
        if case.get("before"):
            case["before"](fx)
        try:
            neg_ok, neg_note = case["verify"](dummy)
        except Exception as e:  # noqa: BLE001
            neg_ok, neg_note = True, f"verifier raised {type(e).__name__}: {e} (treated as False-ish)"
        rows.append({"case_id": case["id"], "negative_pass": (neg_ok is False), "negative_note": neg_note})
        print(f"[{case['id']:4}] negative control: expect False, got {neg_ok!r} -> "
              f"{'PASS' if neg_ok is False else 'FAIL'}  ({neg_note})")

    # Positive controls: apply the real write directly via REST, then assert the verifier flips.
    for row, case in zip(rows, write_cases):
        direct_apply = case.get("direct_apply")
        if not direct_apply:
            row["positive_pass"] = None
            row["positive_note"] = "no direct_apply defined for this case"
            print(f"[{case['id']:4}] positive control: SKIPPED (no direct_apply)")
            continue
        try:
            direct_apply(fx)
        except Exception as e:  # noqa: BLE001
            row["positive_pass"] = False
            row["positive_note"] = f"direct_apply raised {type(e).__name__}: {e}"
            print(f"[{case['id']:4}] positive control: FAIL (direct_apply raised {e})")
            continue
        try:
            pos_ok, pos_note = case["verify"](dummy)
        except Exception as e:  # noqa: BLE001
            pos_ok, pos_note = False, f"verifier raised {type(e).__name__}: {e}"
        row["positive_pass"] = bool(pos_ok)
        row["positive_note"] = pos_note
        print(f"[{case['id']:4}] positive control: expect True, got {pos_ok!r} -> "
              f"{'PASS' if pos_ok else 'FAIL'}  ({pos_note})")

    print("\n================ SELFTEST TABLE ================")
    print(f"{'id':5} {'negative':9} {'positive':9} note")
    bugs = []
    for r in rows:
        neg = "PASS" if r["negative_pass"] else "FAIL"
        pos = ("PASS" if r["positive_pass"] else ("SKIP" if r["positive_pass"] is None else "FAIL"))
        print(f"{r['case_id']:5} {neg:9} {pos:9} {r.get('positive_note', r.get('negative_note', ''))[:100]}")
        if not r["negative_pass"] or r["positive_pass"] is False:
            bugs.append(r["case_id"])

    (OUT_DIR / "selftest.json").write_text(json.dumps(rows, indent=1))
    print(f"\nselftest results written to {OUT_DIR / 'selftest.json'}")
    if bugs:
        print(f"\nVERIFIER BUGS: {bugs}")
    else:
        print("\nAll verifiers passed both controls.")

    teardown(fx)  # prints its own CLEANUP VERIFIED / CLEANUP INCOMPLETE
    return rows


# ---------------------------------------------------------------------------------------------
# Summary: per-case table across passes + headline metrics, printed and written as markdown.
# ---------------------------------------------------------------------------------------------

def percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def write_summary(all_rows, arm, ts):
    scored = [r for r in all_rows if not r.get("skipped")]
    by_case = {}
    for r in scored:
        by_case.setdefault(r["case_id"], []).append(r)

    lines = [f"# Chat quest-ops routing eval -- in-app arm `{arm}` ({ts})", ""]
    lines.append("## Per-case (aggregated across passes)")
    lines.append("")
    lines.append("| id | kind | expect | route(s) | correct | seconds | reads | max_step | notes |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for case_id, rows in by_case.items():
        routes = ",".join(sorted({r.get("route", "?") for r in rows}))
        corrects = "/".join("Y" if r.get("correct") else "N" for r in rows)
        secs = "/".join(str(r.get("seconds", "?")) for r in rows)
        reads = "/".join(str(r.get("read_frame_count", "?")) for r in rows)
        max_steps = "/".join(str(r.get("max_step", "?")) for r in rows)
        note = (rows[0].get("note") or "")[:120].replace("|", "/")
        lines.append(f"| {case_id} | {rows[0].get('kind')} | {rows[0].get('expected')} | "
                     f"{routes} | {corrects} | {secs} | {reads} | {max_steps} | {note} |")

    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    n = len(scored)
    routing_ok = sum(bool(r.get("routing_ok")) for r in scored)
    contrast_rows = [r for r in scored if r.get("kind") == "contrast"]
    contrast_deep = sum(r.get("route") == "delegated" for r in contrast_rows)
    plain_rows = [r for r in scored if r.get("kind") != "contrast"]
    plain_inline = sum(r.get("route") != "delegated" for r in plain_rows)
    write_rows = [r for r in scored if r.get("kind") == "write"]
    writes_landed_inline = sum(bool(r.get("correct")) for r in write_rows
                               if r.get("held_before_yes") is None)
    held_and_landed = sum(bool(r.get("held_before_yes")) and bool(r.get("landed_after_yes"))
                          for r in write_rows if r.get("held_before_yes") is not None)
    read_rows = [r for r in scored if r.get("kind") == "read"]
    reads_correct = sum(bool(r.get("correct")) for r in read_rows)
    list_rows = [r for r in scored if r.get("kind") == "list"]
    listing_correct = sum(bool(r.get("correct")) for r in list_rows)
    all_secs = [r.get("seconds") for r in scored if isinstance(r.get("seconds"), (int, float))]
    step_cap_hits = [r["case_id"] for r in scored if r.get("hit_step_cap")]
    flaky = [cid for cid, rows in by_case.items()
            if len({r.get("correct") for r in rows}) > 1 or len({r.get("route") for r in rows}) > 1]

    lines.append(f"- Routing correct: **{routing_ok}/{n}**")
    lines.append(f"- Contrast cases routed to deep (delegated): **{contrast_deep}/{len(contrast_rows)}**")
    lines.append(f"- Plain ops kept inline (route != delegated): **{plain_inline}/{len(plain_rows)}**")
    if writes_landed_inline or any(r.get("held_before_yes") is None for r in write_rows):
        lines.append(f"- Writes landed inline (auto arm): **{writes_landed_inline}/"
                     f"{sum(1 for r in write_rows if r.get('held_before_yes') is None)}**")
    if held_and_landed or any(r.get("held_before_yes") is not None for r in write_rows):
        lines.append(f"- Writes held then landed after yes (approval arm): **{held_and_landed}/"
                     f"{sum(1 for r in write_rows if r.get('held_before_yes') is not None)}**")
    lines.append(f"- Reads correct: **{reads_correct}/{len(read_rows)}**")
    lines.append(f"- Listing correct: **{listing_correct}/{len(list_rows)}**")
    if all_secs:
        lines.append(f"- Latency: median {round(percentile(all_secs, 50), 1)}s, "
                     f"p90 {round(percentile(all_secs, 90), 1)}s, max {round(max(all_secs), 1)}s")
        for kind in ("read", "list", "write", "inform", "contrast"):
            k_secs = [r["seconds"] for r in scored if r.get("kind") == kind
                     and isinstance(r.get("seconds"), (int, float))]
            if k_secs:
                lines.append(f"  - {kind}: median {round(percentile(k_secs, 50), 1)}s, "
                             f"max {round(max(k_secs), 1)}s (n={len(k_secs)})")
    lines.append(f"- Turns that hit the step cap (max_step >= {STEP_CAP}): "
                f"**{len(step_cap_hits)}** {step_cap_hits}")
    lines.append(f"- Cases that disagree (correct or route) across passes: "
                f"**{len(flaky)}** {flaky}")

    md = "\n".join(lines) + "\n"
    summary_path = OUT_DIR / f"summary_{arm}_{ts}.md"
    summary_path.write_text(md)
    print("\n" + md)
    return summary_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["setup", "selftest", "run", "run-inapp", "teardown"])
    parser.add_argument("--only", default=None, help="comma-separated case ids")
    parser.add_argument("--repeat", type=int, default=2,
                        help="run-inapp only: number of fresh setup->run->teardown passes")
    parser.add_argument("--auto-run", choices=["on", "off"], default="on",
                        help="run-inapp only: 'on' = auto_run=true (Allow all); "
                             "'off' = the app's real default, approval-card arm (write cases only)")
    args = parser.parse_args()
    if args.phase == "setup":
        setup()
    elif args.phase == "selftest":
        selftest()
    elif args.phase == "run":
        run(args.only)
    elif args.phase == "run-inapp":
        run_inapp(args.only, repeat=args.repeat, auto_run_mode=args.auto_run)
    else:
        teardown()


if __name__ == "__main__":
    main()
