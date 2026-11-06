"""Noticing an input that was handled badly: a turn that ended with an unmet goal, an empty reply or work pushed back to the person that the read-only
search could have done becomes a candidate case for the dataset of badly handled inputs, proposed by the hub's Feedback Learning (``noticed``).

Off by default. ``QAR_CANDIDATE_CASES_DIR`` names a private folder outside any repository (the lines hold input and reply text); each candidate appends one
JSON line to ``<dir>/poorly_answered_inputs/<case id>.jsonl`` in the format of the evaluation output log (``dataset``, ``case_id``, ``arm``, ``input``,
``reply``, ``route``, ``score``, ``passed``, ``reason``, ``commit``, ``timestamp``), so a person sets its priority in the datasets sheet. The signals are
structured facts of the turn (the verifier's boolean, the reply being empty, the pursuit that ran), never read from the words of a reply. A write that fails
never changes the turn."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hubofhubs.api import ScopedRecordEdit

from quest_ai_runner.the_body.configuration.runtime_settings import process_environment

ENV = "QAR_CANDIDATE_CASES_DIR"
DATASET = "poorly_answered_inputs"


def turn_signals(verdict: Optional[Mapping[str, Any]], reply: str, pursuit: Optional[Mapping[str, Any]]) -> dict:
    """The structured facts of a finished turn that mean its input was handled badly."""
    unmet = verdict is not None and not verdict.get("met") and not (pursuit or {}).get("met")
    asked_person = bool(verdict) and (str(verdict.get("blocker") or "") == "needs_person" or bool(verdict.get("asks_person")))
    return {"unmet": unmet, "empty_reply": not (reply or "").strip(), "pushed_back": unmet and asked_person and pursuit is not None}


def record_candidate(input_text: str, reply: str, signals: Mapping[str, Any], reason: str = "", environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Appends the candidate case the hub proposes for this turn, when the folder is named and a signal is set; returns the file or None."""
    folder = ((process_environment() if environ is None else environ).get(ENV) or "").strip()
    if not folder:
        return None
    proposed = ScopedRecordEdit().learn("", {"input": input_text, "signals": dict(signals), "reason": reason, "dataset": DATASET,
                                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    edit = proposed.get("edit")
    if not edit:
        return None
    case = edit["value"]
    line = dict(dataset=DATASET, case_id=case["case_id"], arm="feedback-learning:scoped_record_edit", input=case["input"], reply=reply, route="", score=None,
                passed=False, reason=case["reason"], commit="", timestamp=case["timestamp"])
    path = Path(folder).expanduser() / DATASET / (re.sub(r"[^A-Za-z0-9._-]+", "-", case["case_id"]).strip("-") + ".jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return path
