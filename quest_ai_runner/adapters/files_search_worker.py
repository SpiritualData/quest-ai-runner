"""Child process for ``FileOperations.search``: the walk and the regex run here so they can be killed.

The parent (``files_server``) starts this module with a JSON request on stdin and kills it at the
search budget. It prints one JSON line per hit as it finds it (so a killed search still returns what
it had) and a final ``{"done": true, ...}`` line. It holds no secrets: the parent gives it a minimal
environment, and it lowers its own CPU and memory limits before touching a file.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time


def emit(row: dict) -> None:
    sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def limit_resources(budget: float) -> None:
    try:
        import resource
        cpu = int(budget) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
        resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    request = json.loads(sys.stdin.buffer.read() or b"{}")
    budget = float(request["budget"])
    limit_resources(budget)
    if request.get("max_files"):
        os.environ["QAR_GREP_MAX_FILES"] = str(request["max_files"])
    if request.get("skip_dirs"):
        os.environ["QAR_SKIP_DIRS"] = str(request["skip_dirs"])
    from pathlib import Path

    from .files_server import DEFAULT_MAX_HITS, ServedFilesAdapter
    from ._walk import skip_subtrees

    adapter = ServedFilesAdapter(request["root"], deny=request.get("deny") or (),
                                 allow_dot=request.get("allow_dot") or (),
                                 default_grep_max_hits=DEFAULT_MAX_HITS)
    rx = re.compile(request["pattern"], re.IGNORECASE)
    stats = {"files": 0, "stopped": ""}
    deadline = time.monotonic() + budget
    with skip_subtrees(adapter.denied_paths):
        adapter.grep_pass(Path(request["start"]), rx, int(request["limit"]), deadline, stats,
                          on_hit=lambda hit: emit({"hit": hit}))
    emit({"done": True, "files": stats["files"], "stopped": stats["stopped"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
