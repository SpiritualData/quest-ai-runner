"""Score one conversation-recall case: ``ok`` when every labelled relevant conversation is returned and no
forbidden one (another user's conversation) is. A case with no relevant list is a pure leak check. The world
file is pinned by ``world_sha`` in each case (``results.csv`` hashes only ``cases.json``)."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def world():
    return json.loads((HERE / "conversations.json").read_text())


def world_sha():
    return hashlib.sha256((HERE / "conversations.json").read_bytes()).hexdigest()[:8]


def score_case(fn, case):
    if case.get("world_sha") != world_sha():
        raise RuntimeError(f"conversations.json changed (sha {world_sha()}) since the cases were labelled "
                           f"({case.get('world_sha')}); relabel and update world_sha")
    ids = fn(case["query"], case["user"], world()).get("ids", [])
    found = [r for r in case["relevant"] if r in ids]
    leaked = [f for f in case["forbidden"] if f in ids]
    ok = len(found) == len(case["relevant"]) and not leaked
    return dict(ok=ok, leak=bool(leaked), got=",".join(ids) or "(none)",
                note="ok" if ok else (f"leaked {leaked}" if leaked else f"missing {sorted(set(case['relevant']) - set(found))}"))


def aggregate(rows):
    return (f"right conversations recalled {sum(r['ok'] for r in rows)}/{len(rows)}, "
            f"cross-user leaks {sum(bool(r.get('leak')) for r in rows)}")
