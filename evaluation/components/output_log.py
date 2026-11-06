"""The private output log of a run: one JSON line per case, so the reply that earned a score can be read afterwards.

The harness records scores, not replies. When ``QAR_EVAL_OUTPUT_DIR`` names a directory (outside this repository: the lines hold input and reply
text), every ``chat_quality`` and ``end_to_end`` case a run scores, live or replayed, appends one line to ``<dir>/<dataset>/<case id>.jsonl``. A file
is only ever appended to, so each case keeps the history of its runs. Unset (the default), nothing is written and nothing fails; a write that fails
never changes a score.

A line holds: ``dataset``, ``case_id``, ``arm`` (the implementation), ``input``, ``reply``, ``route`` (the route and tool summary when the record has
one), ``score``, ``passed``, ``reason`` (the judge's or scorer's reason), ``commit`` (short sha, ``-dirty`` when the tree has uncommitted changes) and
``timestamp`` (UTC, ISO 8601), then any extra fields the arm gives (the production arm adds ``environment``, ``reads``, ``changes``).
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ENV = "QAR_EVAL_OUTPUT_DIR"
REPO = Path(__file__).resolve().parents[2]


def commit_sha():
    done = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    sha = done.stdout.strip()
    if not sha:
        return ""
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=False)
    return sha + ("-dirty" if dirty.stdout.strip() else "")


def arm_of(fn):
    """The implementation name of an arm function: the module that defines it (``impls/<name>.py``)."""
    return str(getattr(fn, "__module__", "") or "unknown").rsplit(".", 1)[-1]


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "unnamed"


def record(dataset, case_id, arm, input_text, reply, route, score, passed, reason, environ=None, extra=None):
    """Append one line for a case; returns the file written, or None when the variable is unset or the write failed."""
    environ = os.environ if environ is None else environ
    folder = (environ.get(ENV) or "").strip()
    if not folder:
        return None
    line = dict(dataset=dataset, case_id=case_id, arm=arm, input=input_text, reply=reply, route=route, score=score, passed=bool(passed),
                reason=reason, commit=commit_sha(), timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    line = {**{k: v for k, v in (extra or {}).items() if k not in line}, **line}   # an arm's extra fields (environment, reads, would-have writes) never override these
    path = Path(folder).expanduser() / safe_name(dataset) / f"{safe_name(case_id)}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return path
