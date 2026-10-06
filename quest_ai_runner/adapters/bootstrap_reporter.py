"""Bootstrap completion reporting and monthly email notifications.

Tracks the first time a corpus bootstrap completes and sends monthly email reports
with corpus statistics, AI review, and remediation suggestions for any issues found.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)
_BOOTSTRAP_REPORT_STATE_FILE = "bootstrap_report_state.json"


def _get_report_state_file(cards_dir: str) -> Path:
    """Get the path to the bootstrap report state file."""
    return Path(cards_dir) / _BOOTSTRAP_REPORT_STATE_FILE


def _read_report_state(cards_dir: str) -> Dict[str, Any]:
    """Read the bootstrap report state. Returns {} on any error. Never raises."""
    try:
        state_file = _get_report_state_file(cards_dir)
        if state_file.exists():
            return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "first_bootstrap_completed_at": None,
        "last_report_sent_at": None,
        "last_card_count": 0,
        "last_file_count": 0,
    }


def _write_report_state(cards_dir: str, state: Dict[str, Any]) -> None:
    """Write the bootstrap report state atomically. Never raises."""
    try:
        state_file = _get_report_state_file(cards_dir)
        temp_path = state_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp_path.replace(state_file)
    except Exception:  # noqa: BLE001
        pass


def check_should_send_monthly_report(cards_dir: str) -> bool:
    """Check if a monthly report should be sent.

    Returns True if:
    - Bootstrap has completed and this is the first report, OR
    - Last report was sent more than 30 days ago

    Never raises.
    """
    try:
        state = _read_report_state(cards_dir)
        first_completed = state.get("first_bootstrap_completed_at")
        last_sent = state.get("last_report_sent_at")

        if not first_completed:
            return False

        # First report if one has never been sent
        if not last_sent:
            return True

        # Monthly report if last sent more than 30 days ago
        last_sent_dt = datetime.fromisoformat(last_sent)
        now = datetime.now(timezone.utc)
        days_since = (now - last_sent_dt).days
        return days_since >= 30
    except Exception:  # noqa: BLE001
        return False


def mark_bootstrap_completed(cards_dir: str) -> None:
    """Mark that bootstrap has completed for the first time. Never raises."""
    try:
        state = _read_report_state(cards_dir)
        if not state.get("first_bootstrap_completed_at"):
            state["first_bootstrap_completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_report_state(cards_dir, state)
    except Exception:  # noqa: BLE001
        pass


def mark_report_sent(cards_dir: str, card_count: int, file_count: int) -> None:
    """Mark that a monthly report was just sent. Never raises."""
    try:
        state = _read_report_state(cards_dir)
        state["last_report_sent_at"] = datetime.now(timezone.utc).isoformat()
        state["last_card_count"] = card_count
        state["last_file_count"] = file_count
        _write_report_state(cards_dir, state)
    except Exception:  # noqa: BLE001
        pass


def get_corpus_stats(cards_dir: str) -> Dict[str, Any]:
    """Gather statistics about the corpus.

    Returns a dict with:
    - card_count: number of cards indexed
    - file_count: number of files across all cards
    - total_file_size: estimated total size
    - indexed_at: timestamp when gathered
    """
    try:
        cards_path = Path(cards_dir)
        if not cards_path.exists():
            return {
                "card_count": 0,
                "file_count": 0,
                "total_file_size": 0,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }

        card_files = list(cards_path.glob("*.json"))
        card_count = len([f for f in card_files if not f.name.startswith("_")])

        file_count = 0
        for card_file in card_files:
            if card_file.name.startswith("_"):
                continue
            try:
                card_data = json.loads(card_file.read_text(encoding="utf-8"))
                files = card_data.get("files", [])
                file_count += len(files) if isinstance(files, list) else 0
            except Exception:  # noqa: BLE001
                pass

        return {
            "card_count": card_count,
            "file_count": file_count,
            "total_file_size": 0,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # noqa: BLE001
        _log.debug("Failed to gather corpus stats: %s", e)
        return {
            "card_count": 0,
            "file_count": 0,
            "total_file_size": 0,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }


def generate_bootstrap_report(
    cards_dir: str,
    corp_root: str,
    model_provider: Optional[Any] = None,
) -> Dict[str, Any]:
    """Generate a comprehensive monthly bootstrap report.

    Returns a dict with:
    - subject: email subject line
    - body: formatted email body (markdown)
    - stats: corpus statistics dict
    """
    stats = get_corpus_stats(cards_dir)
    prev_state = _read_report_state(cards_dir)

    subject = "Monthly QAR Bootstrap Report"
    body_parts = [
        "# QAR Monthly Bootstrap Report",
        "",
        "## Corpus Statistics",
        "",
        f"- **Cards Indexed**: {stats['card_count']}",
        f"- **Files Tracked**: {stats['file_count']}",
        f"- **Report Generated**: {stats['indexed_at']}",
        "",
    ]

    # Compare with previous report
    prev_card_count = prev_state.get("last_card_count", 0)
    prev_file_count = prev_state.get("last_file_count", 0)

    if prev_card_count > 0:
        card_diff = stats["card_count"] - prev_card_count
        file_diff = stats["file_count"] - prev_file_count
        body_parts.extend([
            "## Changes Since Last Report",
            "",
            f"- Cards: {card_diff:+d} ({'new' if card_diff > 0 else 'removed' if card_diff < 0 else 'unchanged'})",
            f"- Files: {file_diff:+d}",
            "",
        ])

    # Warnings and remediation
    warnings = []
    if stats["card_count"] == 0:
        warnings.append(
            "**No cards indexed**: The corpus may be empty or bootstrap hasn't completed. "
            "Ensure the corpus root is set correctly and contains indexable files."
        )

    if stats["file_count"] == 0 and stats["card_count"] > 0:
        warnings.append(
            "**No files tracked**: Cards were created but no files are associated. "
            "This may indicate a data structure issue."
        )

    if warnings:
        body_parts.extend([
            "## Warnings",
            "",
        ])
        for warning in warnings:
            body_parts.append(f"- {warning}")
        body_parts.append("")

    # Recommendations
    body_parts.extend([
        "## Recommendations",
        "",
        "- Review the card summaries to ensure topics are accurately identified",
        "- Verify file coverage: inspect cards to confirm all important files are indexed",
        "- Use `qar search-context` to test card retrieval and keyword matching",
        "- Check card staleness: cards with many stale files may need re-indexing",
        "",
    ])

    body_parts.extend([
        "---",
        "",
        "This is an automated monthly report from your QAR corpus. "
        "To disable these reports, set the QUEST_BOOTSTRAP_REPORTS environment variable to 0.",
    ])

    body = "\n".join(body_parts)
    return {
        "subject": subject,
        "body": body,
        "stats": stats,
    }
