"""Which open asks this terminal's person has put aside with /notnow, kept across restarts.

The Quest app remembers a folded ask per device (the browser's local storage, key ``quest:ask-folded:<decision id>``). A terminal has no
browser, so its device store is the small state file it already keeps (``QAR_STATE_PATH``, default ``./qar_state.json``), under the
key ``folded_asks``. The ask itself stays open on Quest; this only remembers that the card was folded. Remembering is a convenience:
any failure to read or write leaves the ask open, which is the safe side.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

KEY = "folded_asks"


def state_path() -> Path:
    return Path(os.getenv("QAR_STATE_PATH", "./qar_state.json"))


def folded_ask_ids() -> List[str]:
    try:
        state = json.loads(state_path().read_text())
        ids = state.get(KEY) if isinstance(state, dict) else None
        return [str(i) for i in ids] if isinstance(ids, list) else []
    except (OSError, ValueError):
        return []


def is_folded(decision_id: str) -> bool:
    return bool(decision_id) and decision_id in folded_ask_ids()


def remember_fold(decision_id: str, folded: bool) -> None:
    """Record (or forget) that this ask is folded. The rest of the state file is left as it is."""
    if not decision_id:
        return
    path = state_path()
    try:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            return   # a file this module did not write: never overwrite it
        ids = [i for i in folded_ask_ids() if i != decision_id]
        if folded:
            ids.append(decision_id)
        if ids:
            state[KEY] = ids
        else:
            state.pop(KEY, None)
        path.write_text(json.dumps(state, indent=2))
    except OSError:
        pass
