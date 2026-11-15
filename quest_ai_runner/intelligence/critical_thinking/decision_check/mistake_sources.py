"""Where the reply check's mistakes come from on the Quest side: the chat evaluation's failed cases.

Role: the hub's learning loop (``Mistake``, ``MistakeLog``, ``distill``, ``adopt``) keeps what a check got wrong. A failed case of the chat evaluation (a row of the poor-answers dataset: ``passed``
false, the ``reply`` that was sent, the evaluator's ``reason``, and the structured facts the run recorded) names the view that should have caught it by those facts, never by the words of the reply:
a reply that carries figures the turn never read (``unsupported_figures``) failed the ``reads`` view, with the turn's reads as the input slice; a turn that wrote what it should not have
(``unexpected_writes``) failed the ``actions`` view, with the recorded changes as the slice. A row with neither fact names no view and gives no mistake. The ``right`` field holds the evaluator's
reason, which says what the reply should have held to. The records are the lane's own data (private); only the mechanism is public. Offline, never per request.
"""
import json
from typing import Any, Iterable, List, Mapping

from hubofhubs.api import Mistake, new_mistake

EVAL_FAILURE = "eval_failure"


def row_slice(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if isinstance(value, str):
        return value
    return "\n".join(str(item.get("text") if isinstance(item, Mapping) and "text" in item else item) for item in value or ())


def eval_failure_mistakes(rows: Iterable[Mapping[str, Any]], at: str = "") -> List[Mistake]:
    """The mistakes of the failed cases among ``rows`` (dicts, or JSON lines already parsed)."""
    found = []
    for row in rows:
        if row.get("passed") is not False or not str(row.get("reply") or "").strip():
            continue
        if row.get("unsupported_figures"):
            check, slice_text = "reads", row_slice(row, "reads_seen")
        elif row.get("unexpected_writes"):
            check, slice_text = "actions", row_slice(row, "changes")
        else:
            continue
        found.append(new_mistake(check, slice_text[:2000], str(row["reply"])[:1200], str(row.get("reason") or "")[:600], EVAL_FAILURE, case_id=str(row.get("case_id") or ""), at=at))
    return found


def read_rows(path: str) -> List[Mapping[str, Any]]:
    """Every JSON line of an evaluation output file."""
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]
