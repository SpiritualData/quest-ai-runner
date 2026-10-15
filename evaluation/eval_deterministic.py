"""Thin wrapper kept for the old command. The zero-model checks it used to hold now live behind the
component interface, so solutions can be swapped by name:

    context_selection/  select(task, corpus) -> ranked files       (top-1 / top-3, 15 routing tasks)
    staleness/          changed(before, after) -> stale paths      (precision / recall)

This script prints the cold-start numbers of the shared corpus build, then runs the default solutions of
both components. Same result as running ``components/run.py`` yourself; use that for one solution at a time
(``--impl vector_bge_small`` and ``--impl hybrid_union`` also run offline but embed every file, about 90 s).

    .venv/bin/python3 evaluation/eval_deterministic.py [--all]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "components"))
import common  # noqa: E402
from context_selection.corpus import build_corpus  # noqa: E402

DEFAULT = {"context_selection": ("idf_docstrings", "idf_paths_only", "blind_grep"),
           "staleness": ("store_sha256", "mtime_size")}
EXTRA = {"context_selection": ("vector_bge_small", "hybrid_union")}


def main():
    t0 = time.perf_counter()
    corpus = build_corpus(common.REPO)
    print(f"cold start: {len(corpus.cards)} file cards, {corpus.symbols} symbols, "
          f"{(time.perf_counter() - t0) * 1000:.0f} ms, 0 model calls\n")
    for name, impls in DEFAULT.items():
        score = __import__(f"{name}.score", fromlist=["score_case"])
        for impl in impls + (EXTRA.get(name, ()) if "--all" in sys.argv else ()):
            _, rows = common.run_component(name, score.score_case, ["--impl", impl])
            print(f"{name} / {impl}: {score.aggregate(rows)}\n")


if __name__ == "__main__":
    main()
