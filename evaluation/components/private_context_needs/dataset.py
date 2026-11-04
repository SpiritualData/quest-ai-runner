"""Read the private cases from the path in ``QAR_PRIVATE_CONTEXT_DATASET`` (default: none).

A case line is ``{case_id, domain, source_account_kind, input_text, self_contained, spans_sources,
candidates: [{item_id, type, text, label, why}]}`` where ``label`` is ``needed``, ``helpful`` or ``irrelevant``. Nothing from the data
is ever copied into this repository; this module only reads it from where it lives."""
import json
import os
from pathlib import Path

ENV = "QAR_PRIVATE_CONTEXT_DATASET"
LABELS = ("needed", "helpful", "irrelevant")
WHY_EMPTY = ""


def dataset_file(environ=None):
    """The ``cases.jsonl`` path, or ``(None, reason)`` when the variable is absent or points at nothing."""
    environ = os.environ if environ is None else environ
    raw = (environ.get(ENV) or "").strip()
    if not raw:
        return None, f"{ENV} is not set, so the private dataset is not loaded and this component has no cases"
    path = Path(raw).expanduser()
    if path.is_dir():
        path = path / "cases.jsonl"
    if not path.is_file():
        return None, f"{ENV} points at a path with no cases.jsonl, so this component has no cases"
    return path, ""


def parse_cases(text):
    """Cases from jsonl text; a line that is not a valid case (no id, no candidates list, a label outside LABELS) is skipped."""
    cases = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        cands = raw.get("candidates")
        if not raw.get("case_id") or not isinstance(cands, list) or not all(c.get("label") in LABELS for c in cands):
            continue
        cases.append(dict(id=str(raw["case_id"]), area=raw.get("domain"), task=raw.get("input_text", ""),
                          account=raw.get("source_account_kind", ""), self_contained=bool(raw.get("self_contained")),
                          spans_sources=bool(raw.get("spans_sources")), candidates=cands, heldout=False))
    return cases


def load_cases(environ=None):
    path, why = dataset_file(environ)
    if path is None:
        print(f"private_context_needs: 0 cases loaded: {why}")
        return []
    return parse_cases(path.read_text(encoding="utf-8"))
