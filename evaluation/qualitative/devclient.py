"""Dev-only Quest REST + in-app chat client shared by world.py, judge.py and runner.py.

Copies the minimal helpers from evaluation/chat_quest_ops_routing_eval.py (which is left untouched)
so this package has no import-time dependency on a repo .env. Credentials come from the dev lane's
env file, found relative to this checkout (product/setup/sd-dev-runner/.env) or via
QUAL_DEV_ENV_FILE. The module refuses to load unless the base URL is the dev backend.
"""
import json
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRODUCT = REPO.parents[1]
DEV_ENV_FILE = Path(os.environ.get("QUAL_DEV_ENV_FILE")
                    or PRODUCT / "setup" / "sd-dev-runner" / ".env")

DEV_ENV = {}
for raw_line in DEV_ENV_FILE.read_text().splitlines():
    raw_line = raw_line.strip()
    if raw_line and not raw_line.startswith("#") and "=" in raw_line:
        key, val = raw_line.split("=", 1)
        DEV_ENV[key.strip()] = val.strip().strip('"').strip("'")

QUEST_BASE = DEV_ENV["QUEST_BASE_URL"].rstrip("/")
QUEST_KEY = DEV_ENV["QUEST_API_KEY"]
QUEST_TEAM = DEV_ENV.get("QUEST_TEAM_ID") or ""
assert "batmanhq" in QUEST_BASE and "spiritualdata.org" not in QUEST_BASE, (
    f"REFUSING TO RUN: {QUEST_BASE} is not the dev Quest backend")

TAG = "ZZQEVAL"
WORK_DIR = Path("/tmp/qualeval")
WORK_DIR.mkdir(parents=True, exist_ok=True)


def api(method, path, body=None, params=None, timeout=120, retries=6):
    """One REST call. A 429 (the dev API's rate limit, hit by setup's burst of seeding writes) is
    retried with backoff, honoring Retry-After, instead of surfacing as a failed seed write."""
    for attempt in range(retries + 1):
        status, out, wait = api_once(method, path, body, params, timeout)
        if status != 429 or attempt == retries:
            return status, out
        time.sleep(wait or min(30, 2 ** attempt))
    return status, out


def api_once(method, path, body=None, params=None, timeout=120):
    """(status, body, retry_after_seconds or None)."""
    url = QUEST_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {QUEST_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None), None
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.headers else None
        try:
            retry_after = min(60.0, float(retry_after)) if retry_after else None
        except ValueError:
            retry_after = None
        return e.code, e.read().decode()[:1500], retry_after
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", None


def unwrap_list(body, *keys):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if isinstance(body.get("data"), (dict, list)):
            return unwrap_list(body["data"], *keys)
        for key in keys:
            if body.get(key):
                return body[key]
    return []


def entries_of(collection_id):
    status, body = api("GET", f"/api/data/collections/{collection_id}/entries")
    return unwrap_list(body, "items", "entries") if status == 200 else []


def goals_of(quest_id):
    """Every goal on a quest (the only list route is team-scoped)."""
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


def measurable_outcomes_of(quest_id):
    status, body = api("GET", f"/api/quests/{quest_id}/measurable-outcomes")
    if status != 200:
        return []
    return unwrap_list(body, "outcomes", "items")


def must_get(path, attempts=3):
    """GET for a SNAPSHOT read: retry, then raise. Reading an error as an empty list made a slow
    dev backend look like every quest was created during the case (EX-READ-1, 2026-10-06)."""
    status, body = 0, None
    for attempt in range(attempts):
        status, body = api("GET", path)
        if status == 200:
            return body
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"snapshot read {path} failed after {attempts} tries: {status} {str(body)[:200]}")


def list_collections():
    return unwrap_list(must_get("/api/data/collections"), "collections", "items")


def notes_of(quest_id):
    """A quest's notes, each with ``id`` set. The API names the key ``note_id``; reading ``id``
    collapsed every note into one ``None`` key in snapshots and left teardown/revert unable to
    delete any note (reset then aborted on "still has N notes")."""
    status, body = api("GET", f"/api/quests/{quest_id}/notes")
    notes = unwrap_list(body, "notes", "items") if status == 200 else []
    return [dict(n, id=n.get("id") or n.get("note_id")) for n in notes if isinstance(n, dict)]


def list_quests():
    body = must_get("/api/quests/me")
    if not isinstance(body, list):
        raise RuntimeError(f"/api/quests/me returned {type(body).__name__}, not a list")
    return body


def sse_send(conv_id, content, *, auto_run=True, timeout=600):
    """POST one chat turn to the in-app streaming route and collect every SSE event."""
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


def event_name(event):
    return event.get("event") or event.get("type") or ""


def create_conversation(quest_ids):
    status, body = api("POST", "/api/quest-ai/conversations", {"quest_ids": list(quest_ids)})
    if status != 201:
        raise RuntimeError(f"create conversation failed: {status} {body}")
    return body.get("conversation_id") or body.get("id") or (body.get("data") or {}).get("id")


def delete_conversation(conv_id):
    return api("DELETE", f"/api/quest-ai/conversations/{conv_id}")[0]
