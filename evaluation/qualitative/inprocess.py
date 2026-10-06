"""In-process transport: drive quest-backend's own FastAPI app through TestClient in THIS process.

Why: the dev server's models come from its .env at start and are shared by everyone using dev, so a
per-run model comparison cannot change them. With ``QUAL_INPROCESS=1`` every harness call (REST and
the chat stream) goes to the same app code in this process instead of over HTTP, against the same
dev database, with the tier models pinned for this run only:

    QUAL_INPROCESS=1 QUAL_INPROCESS_MODEL=<model id> <quest-backend python> runner.py run ...

Every call goes in-process, not just the chat: the dev server's in-memory caches would not see this
process's writes, so mixing transports would make snapshots read stale data.

Requirements and guards: the interpreter must be quest-backend's (it imports ``app``); the backend
checkout is ``QUAL_BACKEND_DIR`` (default: a sibling ``quest-backend``); its ``ENVIRONMENT`` must be
development and its Mongo on this machine, or this module refuses to load. The harness's own dev
API key authenticates exactly as over HTTP. Requests are serialized (one TestClient), so run with
``--workers 1``. The app's startup hooks are NOT run (no schedulers or pollers start here).
"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

BACKEND_DIR = Path(os.environ.get("QUAL_BACKEND_DIR")
                   or Path(__file__).resolve().parents[3] / "quest-backend")
TIERS = ("fast", "balanced", "quality", "science", "best")
LOCK = threading.RLock()
STATE = {"client": None, "models": None}


def model_pins():
    """{tier: model} from QUAL_INPROCESS_MODEL (one id for every tier), else {} (the .env models)."""
    model = (os.environ.get("QUAL_INPROCESS_MODEL") or "").strip()
    return {tier: model for tier in TIERS} if model else {}


def start_main_loop():
    """Register a persistent background loop as the app's main loop, as app startup would."""
    from app.core.main_loop import set_main_event_loop
    loop = asyncio.new_event_loop()

    def spin():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=spin, name="qual-main-loop", daemon=True).start()
    set_main_event_loop(loop)


def record_llm_usage():
    """Count every LLM call by model (conceptai's background callback, chained), and write the
    totals to ``<work dir>/inprocess_usage.json`` at exit: proof of which model served the run,
    and its token spend."""
    import atexit
    import conceptai.functions.functions as cff
    from devclient import WORK_DIR
    usage = {}
    previous = getattr(cff, "_llm_background_callback", None)

    def callback(count, record):
        with LOCK:
            row = usage.setdefault(str(record.get("model")), {"calls": 0, "prompt_tokens": 0,
                                                              "completion_tokens": 0})
            row["calls"] += 1
            row["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
            row["completion_tokens"] += int(record.get("completion_tokens") or 0)
        if previous:
            previous(count, record)

    cff.set_llm_background_callback(callback)
    atexit.register(lambda: (WORK_DIR / "inprocess_usage.json").write_text(
        json.dumps({"models": STATE["models"], "usage": usage}, indent=1)))


def client():
    with LOCK:
        if STATE["client"] is not None:
            return STATE["client"]
        if not (BACKEND_DIR / "app").is_dir():
            raise SystemExit(f"QUAL_INPROCESS: {BACKEND_DIR} is not a quest-backend checkout")
        os.chdir(BACKEND_DIR)
        sys.path.insert(0, str(BACKEND_DIR))
        from dotenv import load_dotenv
        load_dotenv(BACKEND_DIR / ".env", override=True)
        if (os.environ.get("ENVIRONMENT") or "").strip().lower() not in ("development", "dev"):
            raise SystemExit("QUAL_INPROCESS: backend ENVIRONMENT is not development; refusing")
        mongo = os.environ.get("MONGODB_URL") or os.environ.get("MONGODB_URI") or ""
        if not mongo.split("@")[-1].split("/")[0].startswith(("localhost", "127.0.0.1")):
            raise SystemExit("QUAL_INPROCESS: backend Mongo is not on this machine; refusing")
        from scripts.checks import routing_eval_core as core
        pins = model_pins()
        core.apply_model_env(pins)  # before the app imports, so config reads see the pins
        from fastapi.testclient import TestClient
        from app.main import app
        try:
            from app.core.llm_config import llm_config
            core.enforce_llm_config(llm_config, pins)  # a later .env load can re-read over them
            STATE["models"] = core.effective_models(llm_config)
        except Exception as e:  # noqa: BLE001
            STATE["models"] = {"error": str(e)}
        start_main_loop()
        record_llm_usage()
        STATE["client"] = TestClient(app, raise_server_exceptions=False)
        print(f"IN-PROCESS backend {BACKEND_DIR.name}, models: {json.dumps(STATE['models'])}")
        return STATE["client"]


def api_once(method, path, key, body=None, params=None, timeout=120):
    """(status, body, retry_after) like devclient.api_once, served by the in-process app."""
    with LOCK:
        resp = client().request(method, path, json=body, params=params,
                                headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    text = resp.text
    if resp.status_code >= 400:
        return resp.status_code, text[:1500], None
    try:
        return resp.status_code, (json.loads(text) if text.strip() else None), None
    except json.JSONDecodeError:
        return resp.status_code, text[:1500], None


def sse_send(path, key, payload, timeout=600):
    """POST one chat turn and collect every SSE ``data:`` event, as devclient.sse_send does."""
    events = []
    with LOCK:
        with client().stream("POST", path, json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}",
                                      "Accept": "text/event-stream"}) as resp:
            if resp.status_code >= 400:
                resp.read()
                return [{"type": "_http_error", "text": f"{resp.status_code}: {resp.text[:400]}"}]
            for line in resp.iter_lines():
                line = (line or "").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    events.append(json.loads(data))
                except json.JSONDecodeError:
                    events.append({"type": "_unparsed", "text": data[:200]})
    return events
