"""Send a quest's mail from inside a run.

A deep run is a subprocess with a shell, not a Python caller, so this is the shape the agent can
actually reach: one command, no library import, credentials from the same env the lane already
uses.

    python -m quest_ai_runner.tools.send_quest_email \\
        --quest quest_abc123 --subject "Friday brief" --body-file /tmp/brief.md --rep bailey

Why this rather than a local mail script, which is what runs did before: mail sent outside Quest
has no per-quest Reply-To, so the person's answer goes nowhere; it misses the account's
unsubscribe handling; it leaves no record on the quest; and it signs as a generic assistant rather
than the persona that wrote it. Recipients are the quest's own setting and cannot be passed here,
so a run chooses what to say, never who hears it.

Exit codes: 0 sent, 1 refused or failed (the reason is printed). A refusal is usually "email is
not enabled for this quest", which is a person's decision to make in the quest's settings, not
something to work around.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from ..runner.quest_client import QuestApiError, QuestClient, QuestNotConfigured


def _body_from(args) -> str:
    if args.body_file:
        return Path(args.body_file).read_text()
    return args.body or ""


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="send_quest_email", description=__doc__)
    parser.add_argument("--quest", required=True, help="quest id the mail belongs to")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", help="message body")
    parser.add_argument("--body-file", help="read the body from a file instead")
    parser.add_argument("--rep", help="AI rep id whose display name signs the mail (e.g. bailey)")
    parser.add_argument("--task", help="assistant task id this came from, recorded for tracing")
    parser.add_argument("--base-url", default=os.getenv("QUEST_API_URL"))
    parser.add_argument("--api-key", default=os.getenv("QUEST_API_KEY"))
    args = parser.parse_args(argv)

    body = _body_from(args).strip()
    if not body:
        print("Nothing to send: --body or --body-file is empty.", file=sys.stderr)
        return 1

    client = QuestClient(base_url=args.base_url, api_key=args.api_key)
    try:
        result = client.send_quest_email(args.quest, subject=args.subject, body=body,
                                         rep_id=args.rep, task_id=args.task)
    except (QuestApiError, QuestNotConfigured) as e:
        print(f"Not sent: {e}", file=sys.stderr)
        return 1

    print(f"Sent as {result.get('persona') or 'Quest AI'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
