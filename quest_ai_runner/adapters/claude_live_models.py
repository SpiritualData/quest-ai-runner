"""Newest live Claude model ids, discovered with the Claude Code subscription login (no API key).

The CLI's ``--model opus`` / ``--model sonnet`` aliases are resolved by the CLI itself and can lag a
release behind (CLI 2.1.288 mapped ``opus`` to ``claude-opus-5`` while ``claude-opus-5-5`` was live).
The Anthropic models list accepts the same OAuth login the CLI already runs on, so a CLI-only lane
can ask it for the newest id of a family and pass that id as ``--model``. Every failure (no login,
expired token, offline) returns ``[]`` and callers fall back to the bare alias.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("quest-ai-runner.claude_live_models")

MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
SUCCESS_TTL_SECONDS = 3600
FAILURE_TTL_SECONDS = 300
DISABLE_ENV = "QAR_CLAUDE_LIVE_MODELS"   # "0" turns the lookup off (tests, air-gapped hosts)

lock = threading.Lock()
cache: dict = {"ids": [], "at": 0.0, "ttl": 0.0}


def credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / ".credentials.json"


def read_access_token() -> Optional[str]:
    try:
        data = json.loads(credentials_path().read_text())
    except Exception:  # noqa: BLE001 — no login file means no live list, never a failure
        return None
    token = (data.get("claudeAiOauth") or data).get("accessToken")
    return token if isinstance(token, str) and token else None


def fetch_ids() -> List[str]:
    token = read_access_token()
    if not token:
        return []
    req = urllib.request.Request(MODELS_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
    except Exception as e:  # noqa: BLE001 — expired token / offline: fall back to the alias
        log.debug("live Claude model list unavailable: %s", e)
        return []
    return [m["id"] for m in payload.get("data", []) if isinstance(m, dict) and m.get("id")]


def live_claude_models() -> List[str]:
    """Live Claude ids, latest first, cached; ``[]`` when unavailable."""
    if os.environ.get(DISABLE_ENV, "").strip() == "0":
        return []
    now = time.monotonic()
    with lock:
        if cache["at"] and now - cache["at"] < cache["ttl"]:
            return cache["ids"]
    ids = fetch_ids()
    with lock:
        cache.update(ids=ids, at=time.monotonic(), ttl=SUCCESS_TTL_SECONDS if ids else FAILURE_TTL_SECONDS)
    return ids


def newest_claude_id(family: str) -> Optional[str]:
    """Newest live id of ``family`` ("opus", "sonnet", "haiku", "fable"), or None."""
    from ..core.model_family import newest_in_family
    return newest_in_family(f"claude-{family}", live_claude_models())
