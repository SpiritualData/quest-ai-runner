"""Re-record the metric rows that are stale, and only those: safe to kill and run again.

Every ``esoi run ... --record`` appends its row to ``evaluation/components/results.csv`` the moment its arm finishes, and a row
counts while no measured code changed since its commit (``qar_overall.combine.current``, the rule of ``esoi check --strict``).
So the rows ARE the progress: this script computes what is stale from them, runs the stale items one at a time, and when it is
killed the next run starts at the item that was in flight, never at the top. Nothing else is stored.

    .venv/bin/python scripts/rerecord.py --plan            # what is current, what would run, nothing runs
    .venv/bin/python scripts/rerecord.py                    # run every stale item (free arms, QME8 owner arms, the two samples)
    .venv/bin/python scripts/rerecord.py --only chat_quality --skip-free

The offline arms are the library's own ``esoi run --stale`` (it is generic: every project that runs metrics with the library, the hub
included, resumes the same way, and ``--plan`` there lists its decisions). This script adds what only QAR knows, in order: the QME8
owner arms (``justified_model_spend``), then the ``chat_quality`` and ``end_to_end`` samples (the Claude subscription; ``chat_quality`` needs ``QAR_EVAL_DEV_ENV_FILE`` and
``QAR_EVAL_DEV_HOST`` set in the environment, passed by path, never read here). Refuses to run while measured code is uncommitted,
because a row recorded on a dirty tree is stale at birth (``--allow-dirty`` overrides). Commit the rows afterwards with the usual
explicit paths; this script commits nothing."""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
COMPONENTS = REPO / "evaluation" / "components"
SAMPLE_SIZE = 10


def latest_rows(results_csv: Path) -> Dict[Tuple[str, str], dict]:
    """The newest row of every (component, impl); the file is append-only, so the last one wins."""
    latest: Dict[Tuple[str, str], dict] = {}
    with open(results_csv, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            latest[(row["component"], row["impl"])] = row
    return latest


def stale_items(items: List[dict], latest: Dict[Tuple[str, str], dict], current: Callable[[str, Optional[dict]], str]) -> List[dict]:
    """The items whose newest row is not current, each with the reason; ``current`` is combine.current (empty string = current)."""
    out = []
    for item in items:
        why = current(item["component"], latest.get((item["component"], item["impl"])))
        item = dict(item, why=why)
        out.append(item)
    return out


def esoi(*args: str) -> List[str]:
    return [sys.executable, "-m", "esoi", *args]


def build_items(latest: Dict[Tuple[str, str], dict], skip_owner: bool, skip_samples: bool) -> List[dict]:
    items: List[dict] = []
    if not skip_owner:
        import json
        manifest = json.loads((COMPONENTS / "justified_model_spend" / "owner_arms.json").read_text(encoding="utf-8"))
        arms = manifest.get("arms", manifest) if isinstance(manifest, dict) else manifest
        for arm in arms:
            key = arm.get("arm") or f"{arm.get('component')}/{arm.get('impl')}"
            component, _, impl = key.partition("/")
            if arm.get("tier") == "api":
                continue
            items.append(dict(kind="owner", component=component, impl=impl,
                              cmd=[sys.executable, str(COMPONENTS / "justified_model_spend" / "record_owner_arms.py"), "--only", key]))
    if not skip_samples:
        backend = os.environ.get("QAR_BACKEND_PYTHON") or str(REPO.parent / "quest-backend" / "venv" / "bin" / "python3")
        env_chat = dict(QUAL_INPROCESS="1", QUAL_INPROCESS_MODEL=os.environ.get("QUAL_INPROCESS_MODEL", "claude-haiku-5-5"),
                        QUAL_BACKEND_DIR=os.environ.get("QUAL_BACKEND_DIR", str(REPO.parent / "quest-backend")),
                        PYTHONPATH=os.pathsep.join([str(REPO), str(REPO.parent / "ai-hub-of-hubs-structure"),
                                                    str(REPO.parent / "evolvable-structure" / "implementations" / "python")]))
        items.append(dict(kind="sample", component="chat_quality", impl="dev_quest_chat", env=env_chat,
                          cmd=[backend, "-m", "esoi", "run", "chat_quality", "--impl", "dev_quest_chat", "--sample", str(SAMPLE_SIZE),
                               "--max-calls", "300", "--record"]))
        items.append(dict(kind="sample", component="end_to_end", impl="qar_live",
                          cmd=esoi("run", "end_to_end", "--impl", "qar_live", "--sample", str(SAMPLE_SIZE), "--max-calls", "200", "--record")))
    return items


def measured_code_is_dirty() -> List[str]:
    """Uncommitted files under the measured code (results and traces are the output, so they do not count)."""
    out = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", "quest_ai_runner", "evaluation/components"],
                         capture_output=True, text=True, check=True).stdout
    skip = ("evaluation/components/results.csv", "evaluation/components/per_case.csv", "evaluation/traces/")
    return [line[3:] for line in out.splitlines() if not line[3:].startswith(skip)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", action="store_true", help="print what is current and what would run; run nothing")
    parser.add_argument("--only", default="", help="run only items whose 'component/impl' contains this text")
    parser.add_argument("--skip-stale", action="store_true", help="skip the library's own `esoi run --stale` (the offline arms)")
    parser.add_argument("--skip-owner", action="store_true")
    parser.add_argument("--skip-samples", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help="record on uncommitted measured code (the rows are stale at birth)")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds one item may run")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(COMPONENTS))
    from qar_overall import combine  # noqa: E402
    latest = latest_rows(REPO / "evaluation" / "components" / "results.csv")
    items = stale_items(build_items(latest, args.skip_owner, args.skip_samples), latest, combine.current)
    if args.only:
        items = [i for i in items if args.only in f"{i['component']}/{i['impl']}"]
    todo = [i for i in items if i["why"]]
    if not args.skip_stale and not args.only:
        # The offline arms: the library decides (current, would run, skipped as paid) and resumes from the recorded rows.
        print("== esoi run --stale" + (" --plan" if args.plan else ""))
        library = subprocess.run(esoi("run", "--stale", *(["--plan"] if args.plan else [])), cwd=REPO, text=True)
        if library.returncode and not args.plan:
            print("the library's stale pass reported an error; continuing with the QAR items")
    print(f"{len(items)} QAR items, {len(items) - len(todo)} current, {len(todo)} to run")
    for item in todo:
        print(f"  {item['kind']:6} {item['component']}/{item['impl']}: {item['why']}")
    if args.plan or not todo:
        return 0
    dirty = measured_code_is_dirty()
    if dirty and not args.allow_dirty:
        print("refusing: measured code is uncommitted (a row recorded now is stale at birth):", *dirty[:8], sep="\n  ")
        return 2
    failed = 0
    for number, item in enumerate(todo, 1):
        started = time.time()
        print(f"[{number}/{len(todo)}] {item['component']}/{item['impl']} ...", flush=True)
        try:
            done = subprocess.run(item["cmd"], cwd=REPO, env=dict(os.environ, **item.get("env", {})), timeout=args.timeout,
                                  capture_output=True, text=True)
            status = "ok" if done.returncode == 0 else f"exit {done.returncode}"
        except subprocess.TimeoutExpired:
            status = "timed out"
        failed += status != "ok"
        print(f"    {status} in {time.time() - started:.0f}s", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
