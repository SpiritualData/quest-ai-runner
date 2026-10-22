"""Card-store commands: ``quest-ai-runner cards report``.

Split out of ``cli.py`` by definition, one module per command family; that module re-exports every name here.
"""
from __future__ import annotations

import os
from pathlib import Path


def run_cards_command(args, log) -> int:
    """Run the `cards` subcommand; returns the process exit code."""
    from .adapters import card_limit
    from .adapters.card_limit_report import report_for, write_report

    corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
    cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")
    if not os.path.isdir(cards_dir):
        print(f"error: {cards_dir} is not a card store (a folder of card files). Pass --cards-dir.")
        return 2
    text = report_for(cards_dir, card_limit.max_cards_from_env())
    print(text)
    if args.write:
        from .adapters.card_limit_report import REPORT_NAME
        if write_report(Path(cards_dir), text):
            print(f"Written to {Path(cards_dir) / REPORT_NAME}")
    return 0
