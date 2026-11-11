"""The `spend-by-role` subcommand: what each model call role cost, from the per-call log the runner already keeps (read only)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.call_roles import render_spend_report, spend_by_role
from quest_ai_runner.the_world.model_plumbing.spend.usage import read_call_log


def add_spend_command(sub) -> None:
    """The `spend-by-role` subcommand."""
    p = sub.add_parser("spend-by-role", help="spend by call role and tier over a period, from the per-call log (read only)")
    p.add_argument("--log", default=None, metavar="FILE", help="the call log (default: QAR_CALL_LOG_PATH)")
    p.add_argument("--days", type=float, default=7.0, help="the period, counted back from now (default 7)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")


def _cmd_spend_by_role(args, log) -> int:
    path = args.log or os.getenv("QAR_CALL_LOG_PATH") or ""
    if not path:
        print("No call log: pass --log FILE or set QAR_CALL_LOG_PATH to the lane's call log.")
        return 2
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
    report = spend_by_role(read_call_log(path), since=since)
    print(json.dumps(report, indent=2) if args.json else (render_spend_report(report) if report else "The call log holds no call in this period."))
    return 0
