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

from .consumer_check import check_consumer, library_dir
from .records import check_records, load_template, read_csv


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
    rec = sub.add_parser("check-records", help="verify intent records (problems, solutions, implementations, ideals, metrics)")
    rec.add_argument("--dir", default="docs/structure", help="the folder holding problems.csv, ideals.csv, solutions.csv, implementations.csv, metrics.csv")
    rec.add_argument("--template", default=None, help="the parsed ESOI template (default: the library's docs/structure/esoi_template.csv)")
    rec.add_argument("--related-problems", default=None, help="another registry's problems.csv that related_problem_ids must resolve in "
                     "(default: the library's, when found)")
    rec.add_argument("--waive", default="", help="comma-separated solution ids whose records are still to be written")
    args = parser.parse_args(argv)
    if args.cmd == "check-records":
        source = library_dir().parent / "docs" / "structure"
        template = Path(args.template) if args.template else source / "esoi_template.csv"
        if not template.exists():
            print(f"note: template {template} not found (a wheel install has no docs); records not checked")
            return 0
        related = Path(args.related_problems) if args.related_problems else source / "problems.csv"
        known = {row["problem_id"] for row in read_csv(related)} if related.exists() else None
        problems = check_records(Path(args.dir), load_template(template), {w for w in args.waive.split(",") if w},
                                 related_problem_ids=known)
        for line in problems:
            print(line)
        return 1 if problems else 0
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
