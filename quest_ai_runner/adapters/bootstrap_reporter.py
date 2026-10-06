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
from typing import Any, Callable, Dict, Optional

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
    corpus_root: str,
    model_provider: Optional[Any] = None,
) -> Dict[str, Any]:
    """Generate a comprehensive monthly bootstrap report with professional formatting.

    Returns a dict with:
    - subject: email subject line
    - body: formatted email body (markdown, professional)
    - stats: corpus statistics dict
    - issues: list of any issues detected
    - recommendations: list of actionable recommendations
    """
    stats = get_corpus_stats(cards_dir)
    prev_state = _read_report_state(cards_dir)

    # Format timestamp for readability
    indexed_dt = datetime.fromisoformat(stats["indexed_at"])
    formatted_date = indexed_dt.strftime("%B %d, %Y at %I:%M %p UTC")

    subject = "QAR Corpus Monthly Summary"

    # Build header with summary statistics
    body_parts = [
        "# QAR Corpus Monthly Summary",
        "",
    ]

    # Current state statistics
    body_parts.extend([
        "## Current Indexing State",
        "",
        f"**Indexed Cards**: {stats['card_count']} topic cards describing your corpus",
        "",
        f"**Files Tracked**: {stats['file_count']} files across indexed topics",
        "",
        f"**Report Generated**: {formatted_date}",
        "",
    ])

    # Month-over-month comparison
    prev_card_count = prev_state.get("last_card_count", 0)
    prev_file_count = prev_state.get("last_file_count", 0)

    if prev_card_count > 0:
        card_diff = stats["card_count"] - prev_card_count
        file_diff = stats["file_count"] - prev_file_count

        body_parts.append("## Changes Since Last Report")
        body_parts.append("")

        if card_diff > 0:
            body_parts.append(f"**New Cards**: +{card_diff} topic cards identified")
        elif card_diff < 0:
            body_parts.append(f"**Removed Cards**: {card_diff} topic cards removed")
        else:
            body_parts.append("**Cards**: No change since last report")

        if file_diff > 0:
            body_parts.append(f"**New Files**: +{file_diff} files indexed")
        elif file_diff < 0:
            body_parts.append(f"**Removed Files**: {file_diff} files removed")

        body_parts.append("")
    elif prev_card_count == 0 and stats["card_count"] > 0:
        body_parts.extend([
            "## First Report",
            "",
            f"This is your first bootstrap report. {stats['card_count']} topic cards have been identified and indexed.",
            "",
        ])

    # AI Review and Issues
    issues = []
    recommendations = []

    if stats["card_count"] == 0:
        issues.append(
            "No cards have been indexed. Your corpus may not contain indexable files, "
            "or the corpus root is not correctly configured."
        )
        recommendations.append(
            "Verify the corpus root path contains markdown, code, or text files with content to index."
        )

    if stats["card_count"] > 0 and stats["file_count"] == 0:
        issues.append(
            "Cards were created but no files are associated. This indicates a data structure issue "
            "where cards exist but file tracking failed."
        )
        recommendations.append(
            "Check the card files in the .quest-context directory to verify file entries are present."
        )

    if stats["card_count"] > 200:
        issues.append(
            f"Your corpus has grown to {stats['card_count']} cards. At this scale, consider whether "
            "the corpus structure can be better organized to reduce card proliferation."
        )
        recommendations.append(
            "Review and consolidate related cards to improve retrieval relevance and reduce overhead."
        )
    elif stats["card_count"] > 100:
        recommendations.append(
            "You have a substantial corpus. Periodically review cards to ensure topics remain accurate and distinct."
        )

    if stats["file_count"] > 5000:
        recommendations.append(
            "Your indexed file count is high. Consider whether all files are necessary, "
            "or whether filtering/exclusion rules might improve performance."
        )

    if issues:
        body_parts.extend([
            "## Issues Detected",
            "",
        ])
        for issue in issues:
            body_parts.append(f"**Action Required**: {issue}")
            body_parts.append("")

    if recommendations:
        body_parts.extend([
            "## Recommendations",
            "",
        ])
        for rec in recommendations:
            body_parts.append(f"- {rec}")
        body_parts.append("")

    # Next steps
    body_parts.extend([
        "## Next Steps",
        "",
        "- Test your retrieval with: `quest-ai-runner search-context --query \"your test query\"`",
        "- Review indexed topics to confirm accuracy: `quest-ai-runner bootstrap --check`",
        "- Monitor corpus performance in your QAR deployments",
        "",
    ])

    # Footer
    body_parts.extend([
        "---",
        "",
        "This monthly report is automated, generated by your QAR corpus indexing system. ",
        "It includes statistics about indexed content and suggestions for optimization.",
    ])

    body = "\n".join(body_parts)
    return {
        "subject": subject,
        "body": body,
        "stats": stats,
        "issues": issues,
        "recommendations": recommendations,
    }


def send_bootstrap_report_via_quest(
    cards_dir: str,
    corpus_root: str,
    user_id: str,
    quest_client_factory: Callable[[], Any],
    model_provider: Optional[Any] = None,
) -> bool:
    """Send the monthly bootstrap report via Quest email if conditions are met.

    Sends an email only if:
    1. Bootstrap has completed at least once
    2. No email has been sent in the last 30 days
    3. A valid quest_client can be created

    Returns True if an email was sent, False otherwise. Never raises.
    """
    try:
        if not check_should_send_monthly_report(cards_dir):
            return False

        # Generate the report
        report = generate_bootstrap_report(cards_dir, corpus_root, model_provider)

        # Send via Quest if client is available
        try:
            quest_client = quest_client_factory()
            quest_client.send_email(
                to=[user_id],
                subject=report["subject"],
                body=report["body"],
                quest_id=None,
            )
            # Mark as sent
            mark_report_sent(
                cards_dir,
                report["stats"]["card_count"],
                report["stats"]["file_count"],
            )
            _log.info(
                "context index: monthly bootstrap report sent to %s "
                "(cards=%d, files=%d)",
                user_id,
                report["stats"]["card_count"],
                report["stats"]["file_count"],
            )
            return True
        except Exception as e:  # noqa: BLE001
            _log.debug("Failed to send bootstrap report via Quest: %s", e)
            return False

    except Exception as e:  # noqa: BLE001
        _log.debug("Error in send_bootstrap_report_via_quest: %s", e)
        return False
