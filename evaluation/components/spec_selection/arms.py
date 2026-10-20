"""Shared by the arms: the fixture index (the real output of ``esoi spec index``), the real cheap model judge and the real
vector search, and the one runner that scores a request. Nothing here is scripted: a request is answered by the code a run
would use, over the index it would use, and the case says which records the request concerns."""
import json
import os
import shutil
from pathlib import Path

import common  # noqa: F401  (sets sys.path for the repo)

INDEX = json.loads((Path(__file__).resolve().parent / "fixture_index.json").read_text())
JUDGE_MODEL = os.environ.get("QAR_EVAL_MODEL", "haiku")


def claude_path():
    """The ``claude`` CLI this machine is logged in to. A missing one is a visible failure, not a zero score."""
    found = os.environ.get("QAR_EVAL_CLAUDE") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(found).is_file():
        raise RuntimeError("no claude CLI to judge with: this metric needs a real model call and will not substitute a scripted one")
    return found


def real_provider(meter):
    """The runner's own keyless model client over the logged-in ``claude`` CLI, every call counted by the meter."""
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    return meter.wrap(ClaudeCliProvider(claude_path=claude_path(), timeout_seconds=120))


def real_vector_search():
    """``vector_search(text, k) -> [(id, cosine)]`` by cosine similarity of local bge-small embeddings of each entry's card line.
    The model is the one the context-selection evaluation uses (read offline from the fastembed cache); it raises when the model
    is not cached, never degrades to no vector search. No on-disk vector cache: a few hundred short lines embed in seconds."""
    import numpy as np
    from context_selection.embedder import MODEL
    from fastembed import TextEmbedding
    from quest_ai_runner.core.spec_selection import card_line
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"))

    def embed(texts):
        return np.array([[float(x) for x in v] for v in model.embed(texts)])
    ids = [e["id"] for e in INDEX["entries"]]
    matrix = embed([card_line(e) for e in INDEX["entries"]])
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    def search(text, k):
        q = embed([text])[0]
        scores = matrix @ (q / np.linalg.norm(q))
        return [(ids[i], float(scores[i])) for i in np.argsort(-scores)[:k]]
    return search


def runner(select, meter, *, judge=False, vector=False):
    provider = real_provider(meter) if judge else None
    vector_search = real_vector_search() if vector else None

    def run(kind, args):
        got = select(args["task"], INDEX, provider=provider, model=JUDGE_MODEL if judge else None, vector_search=vector_search)
        selected = [i for i in got.ids if i not in got.linked]
        return {"ids": selected, "linked": [i for i in got.ids if i in got.linked], "n_ids": len(got.ids), "n_selected": len(selected), "route": got.route, "calls": got.calls, "failed_batches": got.failed_batches}
    return run
