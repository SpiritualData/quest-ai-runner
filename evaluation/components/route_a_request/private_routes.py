"""Private requests with a route label, as a conditional case source of the route-a-request metric.

The cases are requests of the private chat-quality datasets (the poorly answered inputs and the context-selection cases) labelled with the route a reader says they ask for. The text
stays in the datasets, OUTSIDE this repository, and the labels in ``route_labels.jsonl`` beside them (``source``, ``case_id``, ``route``, ``basis``: no request text), found through
``QAR_PRIVATE_POOR_ANSWERS_DATASET`` and ``QAR_PRIVATE_CONTEXT_DATASET`` (files or folders, several separated by ``os.pathsep``). With none present the source contributes no case and
nothing fails. Nothing from the data is copied here: the digest of the cases is the sha256 of every present file, written into the untracked ``datasets/case_sources.local.json`` (only when
a file is present) before the case files are hashed, so a row recorded with the set and a row recorded without it never compare. Request text is never written to a result or a log: a
row carries the case id and the routes only.
"""
import hashlib
import json
import os
from pathlib import Path

ENVS = ("QAR_PRIVATE_POOR_ANSWERS_DATASET", "QAR_PRIVATE_CONTEXT_DATASET")
LABELS = "route_labels.jsonl"
TEXTS = {"poorly_answered_inputs": "poorly_answered_inputs.jsonl", "cases": "cases.jsonl"}
SOURCE = "route_labels"
ABSENT = "private:absent"
HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "datasets" / "case_sources.local.json"      # untracked (gitignored); the committed case_sources.json always says ``private:absent``


def folders(environ=None):
    """The folders of the private datasets named by the environment that hold a label file."""
    environ = os.environ if environ is None else environ
    found = []
    for name in ENVS:
        for raw in (environ.get(name) or "").split(os.pathsep):
            path = Path(raw.strip()).expanduser() if raw.strip() else None
            folder = path.parent if path is not None and path.is_file() else path
            if folder is not None and (folder / LABELS).is_file() and folder not in found:
                found.append(folder)
    return found


def digest_part(environ=None):
    """``private:absent`` or ``route_labels:<sha256 of the label and text files present>`` joined with ``+`` in sorted order."""
    shas = sorted(hashlib.sha256((folder / name).read_bytes()).hexdigest() for folder in folders(environ) for name in (LABELS, *TEXTS.values()) if (folder / name).is_file())
    return f"{SOURCE}:{'+'.join(shas)}" if shas else ABSENT


def publish_digest(environ=None):
    part = digest_part(environ)
    if part == ABSENT:
        SOURCES_FILE.unlink(missing_ok=True)
        return
    text = json.dumps({SOURCE: part}) + "\n"
    if not SOURCES_FILE.exists() or SOURCES_FILE.read_text(encoding="utf-8") != text:
        SOURCES_FILE.write_text(text, encoding="utf-8")


def rows_of(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def load_cases(environ=None):
    """One case per labelled request found in the datasets present; none when nothing is present. A label with no text, or text with no label, is skipped."""
    publish_digest(environ)
    seen, cases = set(), []
    for folder in folders(environ):
        texts = {(source, row.get("case_id")): str(row.get("input_text") or "").strip() for source, name in TEXTS.items() for row in rows_of(folder / name)}
        for label in rows_of(folder / LABELS):
            key = (label.get("source"), label.get("case_id"))
            if key in seen or not texts.get(key) or label.get("route") not in ("information", "act", "draft", "clarify"):
                continue
            seen.add(key)
            cases.append({"id": f"private-{label['case_id']}", "kind": label["route"], "source": SOURCE, "private": True,
                          "args": {"request": texts[key], "context": {}}, "expect": label["route"]})
    return cases
