"""Offline baseline: how much do past deep runs look around before they first change anything?

Finds the saved Claude Code session records of past deep runs, runs the hub's ``exploration_account`` on each (no model, no network),
and prints and writes only numbers: how many sessions, the median and mean of looking tool uses before the first write (any change, shell commands that do more than look included) and
before the first file edit, the share of
all tool uses that only look, and the share of sessions that never wrote. Nothing from a transcript is printed or written.

A deep run is told apart from an interactive session by two marks of the launch prompt this library composes: the session was started
headless (its ``entrypoint`` is ``sdk-cli``) and its first message carries the ``TASK [<8 hex>]:`` label. A session whose first
message cannot be read is skipped. Sessions this account cannot read are skipped silently.

    python evaluation/components/deep_runner/exploration_baseline.py [--limit 60] [--min-calls 5] [--config-dir DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from typing import Dict, List, Optional

from hubofhubs.api import session as hub

TASK_LABEL = re.compile(r"TASK(?: \d+)? \[[0-9a-f]{8}\]:")
HEAD_LINES = 40
DEFAULT_OUT = "exploration_baseline.json"


def session_files(config_dir: Optional[str] = None) -> List[str]:
    """Every session record under the config directory's ``projects`` folder, newest first."""
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    found = glob.glob(os.path.join(base, "projects", "*", "*.jsonl"))
    stamped = []
    for path in found:
        try:
            stamped.append((os.path.getmtime(path), path))
        except OSError:
            continue
    return [p for _, p in sorted(stamped, reverse=True)]


def is_deep_run(path: str) -> bool:
    """Whether the record's opening lines show a headless session launched with this library's TASK label."""
    headless = False
    labelled = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh):
                if number >= HEAD_LINES:
                    break
                if '"sdk-cli"' in line:
                    headless = True
                if TASK_LABEL.search(line):
                    labelled = True
    except OSError:
        return False
    return headless and labelled


def baseline(config_dir: Optional[str] = None, limit: int = 60, min_calls: int = 5, max_scan: int = 5000) -> Dict[str, object]:
    accounts = []
    scanned = 0
    for path in session_files(config_dir):
        if len(accounts) >= limit or scanned >= max_scan:
            break
        scanned += 1
        if not is_deep_run(path):
            continue
        account = hub.exploration_account(path)
        if account["tool_calls_total"] >= min_calls:
            accounts.append(account)
    before = [a["explore_before_first_write"] for a in accounts]
    calls = sum(a["tool_calls_total"] for a in accounts)
    looks = sum(a["explore_total"] for a in accounts)
    n = len(accounts)
    before_file = [a["explore_before_first_file_change"] for a in accounts]
    return {
        "n": n, "sessions_scanned": scanned, "min_tool_calls": min_calls,
        "explore_before_first_write": {"median": statistics.median(before) if before else None,
                                       "mean": round(statistics.mean(before), 2) if before else None},
        "explore_before_first_file_change": {"median": statistics.median(before_file) if before_file else None,
                                             "mean": round(statistics.mean(before_file), 2) if before_file else None},
        "share_never_changed_a_file": round(sum(1 for a in accounts if not a["files_changed"]) / n, 3) if n else None,
        "explore_share_of_tool_calls": round(looks / calls, 3) if calls else None,
        "share_never_wrote": round(sum(1 for a in accounts if a["first_write_index"] is None) / n, 3) if n else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--min-calls", type=int, default=5)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--out", default=DEFAULT_OUT, help="where to write the JSON; a bare name is written to the current folder")
    args = parser.parse_args(argv)
    result = baseline(args.config_dir, args.limit, args.min_calls)
    text = json.dumps(result, indent=2)
    print(text)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
