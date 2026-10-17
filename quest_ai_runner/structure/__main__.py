"""``python -m quest_ai_runner.structure check-consumer --registry <csv>``: verify a consumer registry.

    python -m quest_ai_runner.structure check-consumer --registry docs/structure/qar_integration.csv \
        --root . --requirements requirements.txt [--solutions s.csv --results r.csv] [--fail-on-lag]

Exit codes: 0 clean, 1 violations, 2 usage. Pin lag is reported but only fails with ``--fail-on-lag``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .consumer_check import check_consumer


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quest_ai_runner.structure", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check-consumer", help="verify a consumer's QAR integration registry")
    check.add_argument("--registry", required=True, help="the consumer's registry CSV")
    check.add_argument("--root", default=".", help="the consumer repo root (put on sys.path; tests resolve from it)")
    check.add_argument("--requirements", default=None, help="a requirements file naming the pinned QAR commit")
    check.add_argument("--fail-on-lag", action="store_true", help="exit 1 when the pin is behind the installed checkout")
    check.add_argument("--solutions", default=None, help="the consumer's solutions CSV (with --results: C9 and C10)")
    check.add_argument("--results", default=None, help="the consumer's recorded results CSV")
    args = parser.parse_args(argv)
    report = check_consumer(Path(args.registry), Path(args.root), Path(args.requirements) if args.requirements else None,
                            Path(args.solutions) if args.solutions else None, Path(args.results) if args.results else None)
    for line in report.problems:
        print(line)
    for line in report.notes:
        print(f"note: {line}")
    if report.counts:
        print("rows by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())))
    failed = bool(report.problems) or (args.fail_on_lag and report.pin_lag)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
