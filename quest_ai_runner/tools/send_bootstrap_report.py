"""Send a QAR bootstrap monthly report via Quest email.

This tool generates a monthly report about the corpus and sends it to the authenticated user
via Quest's email system. The report includes statistics, changes since the last report,
warnings, and remediation recommendations.

    python -m quest_ai_runner.tools.send_bootstrap_report \\
        --cards-dir /path/to/.quest-context \\
        --corp-root /path/to/corpus \\
        --quest quest_abc123

Exit codes: 0 sent, 1 refused or failed (the reason is printed).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from ..adapters.bootstrap_reporter import (
    check_should_send_monthly_report,
    generate_bootstrap_report,
    mark_report_sent,
    get_corpus_stats,
)
from ..runner.quest_client import QuestApiError, QuestClient, QuestNotConfigured


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="send_bootstrap_report", description=__doc__)
    parser.add_argument("--cards-dir", required=True, help="Path to the .quest-context directory")
    parser.add_argument("--corp-root", required=True, help="Path to the corpus root directory")
    parser.add_argument("--quest", required=True, help="Quest id to send the report to")
    parser.add_argument("--force", action="store_true", help="Send report even if not due yet")
    parser.add_argument("--base-url", default=os.getenv("QUEST_BASE_URL") or os.getenv("QUEST_API_URL"))
    parser.add_argument("--api-key", default=os.getenv("QUEST_API_KEY"))
    args = parser.parse_args(argv)

    # Check if report should be sent
    if not args.force and not check_should_send_monthly_report(args.cards_dir):
        print("Monthly report not due yet.", file=sys.stderr)
        return 1

    # Generate the report
    report = generate_bootstrap_report(args.cards_dir, args.corp_root)
    stats = get_corpus_stats(args.cards_dir)

    # Send via Quest
    client = QuestClient(base_url=args.base_url, api_key=args.api_key)
    try:
        result = client.send_quest_email(
            args.quest,
            subject=report["subject"],
            body=report["body"],
            rep_id="qar",
        )
    except (QuestApiError, QuestNotConfigured) as e:
        print(f"Not sent: {e}", file=sys.stderr)
        return 1

    # Mark that we sent the report
    mark_report_sent(args.cards_dir, stats["card_count"], stats["file_count"])
    print(f"Report sent: {report['subject']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
