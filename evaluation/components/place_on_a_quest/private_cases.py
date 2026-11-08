"""Real placements as a conditional case source of the one ``place_on_a_quest`` metric.

The cases are real messages a person sent, each labelled by that person (or a reviewer) with the quest ids it belongs to (the empty list when none
did) and carrying the quests it was labelled against. They are kept OUTSIDE this repository, in ``placement_cases.jsonl`` in the file or folder
named by ``QAR_PRIVATE_PLACEMENT_DATASET`` (several separated by ``os.pathsep``); one JSON object per line:
``{"id", "message", "previous", "quests": [{"quest_id", "outcome", "category", "state", "scope", "open": [], "vision"?, "purpose"?}],
"expect": [quest ids]}`` (``vision`` and ``purpose`` are optional; see ``account``).
With nothing present the source contributes no case and the metric runs on the public cases alone; nothing fails. Nothing from the data is copied
into the repository. The private poorly answered inputs (``QAR_PRIVATE_POOR_ANSWERS_DATASET``) name what a cue resolves to in words, not by quest id,
so they become cases here once a person has labelled them against the quests they came from.

The share of this source in the one number is ``source_shares`` of ``[balance.place_on_a_quest]`` in esoi.toml, fixed before any case is scored."""
import json
import os
from pathlib import Path

ENV = "QAR_PRIVATE_PLACEMENT_DATASET"
FILE_NAME = "placement_cases.jsonl"
SOURCE = "real"


def data_files(environ=None):
    environ = os.environ if environ is None else environ
    found = []
    for raw in (environ.get(ENV) or "").split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip()).expanduser()
            path = path / FILE_NAME if path.is_dir() else path
            if path.is_file():
                found.append(path)
    return found


def load_cases(environ=None):
    cases, seen = [], set()
    for path in data_files(environ):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            ident = f"REAL-{row['id']}"
            if ident in seen or not row.get("quests"):
                continue
            seen.add(ident)
            cases.append({"id": ident, "source": SOURCE, "message": row["message"], "previous": row.get("previous") or "",
                          "expect": list(row.get("expect") or []), "quests": row["quests"]})
    return cases


def share(table=None):
    """The share of this source in the one number, from ``[balance.place_on_a_quest]`` (``source_shares``)."""
    if table is None:
        import common
        table = common.CONFIG.balance.get("place_on_a_quest") or {}
    value = (table.get("source_shares") or {}).get(SOURCE)
    if value is None or not 0 < float(value) < 1:
        raise ValueError(f"[balance.place_on_a_quest] source_shares needs {SOURCE} between 0 and 1, got {value!r}")
    return float(value)
