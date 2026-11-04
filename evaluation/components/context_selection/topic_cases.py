"""Topic cases of the context-selection metric: tasks from QAR's own git history that name no file.

A commit that changed two to eight Python files of the ``quest_ai_runner`` package is a task when its subject is a request in words: the subject
names no path, file name, extension or code identifier (a person asking for the change would not know them). The files the commit changed that
exist in today's corpus are the needed set, and the case passes when at least half of them are among the first ten files the arm ranks. The
history is read with the hub's helpers (``cards_from_files.topic_cases``: ``history_of``, a read-only ``git log`` of the commits before the commit
the hub's cards-from-files pin names, and ``names_something``, the subject filter); ``topic_cases.json`` is the committed sample and ``topic_cases_pin.json`` holds its sha256, checked on every load.
``python3 -m context_selection.topic_cases`` writes both from the sibling checkout and refuses to replace a pinned file.
"""
import hashlib
import json
import sys
from pathlib import Path

import common

HERE = Path(__file__).resolve().parent
CASES_FILE, PIN_FILE = HERE / "topic_cases.json", HERE / "topic_cases_pin.json"
PACKAGE = "quest_ai_runner/"
MIN_CHANGED, MAX_CHANGED, MIN_SUBJECT, MIN_NEEDED, SAMPLE_SIZE = 2, 8, 30, 2, 60


def hub_helpers():
    """The hub's history reader and subject filter, and the commit its pin names, found beside the hubofhubs package (its evaluation folder)."""
    import hubofhubs
    evaluation = Path(hubofhubs.__file__).resolve().parents[1] / "evaluation"
    if str(evaluation) not in sys.path:
        sys.path.insert(0, str(evaluation))
    from cards_from_files import corpus as pinned
    from cards_from_files import topic_cases as hub_cases
    return hub_cases.history_of, hub_cases.names_something, pinned.read_pin()["qar_commit"]


def derive(commits, corpus_files, names_something, size=SAMPLE_SIZE):
    known, cases = set(corpus_files), []
    for entry in commits:
        changed = [f for f in entry["files"] if f.startswith(PACKAGE) and f.endswith(".py")]
        if not (MIN_CHANGED <= len(changed) <= MAX_CHANGED and len(entry["subject"]) >= MIN_SUBJECT and not names_something(entry["subject"])):
            continue
        needed = sorted(f for f in changed if f in known)
        if len(needed) >= MIN_NEEDED:
            cases.append(dict(id=f"TOP-{entry['commit'][:8]}", area="topic", task=entry["subject"], needed=needed, heldout=len(cases) % 5 == 4))
        if len(cases) == size:
            break
    return cases


def load():
    """The committed topic cases, after checking the file against its pin."""
    text = CASES_FILE.read_text(encoding="utf-8")
    pin = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != pin["cases_sha256"]:
        raise RuntimeError(f"{CASES_FILE.name} does not match {PIN_FILE.name}: the topic cases are pinned and never regenerated silently")
    return json.loads(text)["cases"]


def write(replace=False):
    if PIN_FILE.exists() and not replace:
        raise RuntimeError(f"{PIN_FILE.name} exists: pass --replace to replace the pinned topic cases on purpose")
    from context_selection.corpus import build_corpus
    history, names_something, commit = hub_helpers()
    cases = derive(history(common.REPO, commit), build_corpus(common.REPO).files, names_something)
    text = json.dumps(dict(before_commit=commit, rule="a commit subject that names no path, file name, extension or code identifier is the task; "
                           "the package files the commit changed that are in today's corpus are the needed set", cases=cases),
                      indent=1, ensure_ascii=False) + "\n"
    CASES_FILE.write_text(text, encoding="utf-8")
    pin = dict(before_commit=commit, cases_file=CASES_FILE.name, cases_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), cases=len(cases))
    PIN_FILE.write_text(json.dumps(pin, indent=1) + "\n", encoding="utf-8")
    return pin


if __name__ == "__main__":
    print(write(replace="--replace" in sys.argv))
