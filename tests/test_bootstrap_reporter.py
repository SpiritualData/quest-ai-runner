"""Tests for bootstrap reporter: monthly email summaries and state tracking."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quest_ai_runner.adapters.bootstrap_reporter import (
    check_should_send_monthly_report,
    generate_bootstrap_report,
    get_corpus_stats,
    mark_bootstrap_completed,
    mark_report_sent,
    send_bootstrap_report_via_quest,
)


def test_no_report_before_bootstrap_completed():
    """Should not send a report if bootstrap hasn't completed yet."""
    with tempfile.TemporaryDirectory() as tmpdir:
        should_send = check_should_send_monthly_report(tmpdir)
        assert not should_send


def test_first_report_after_bootstrap_completed():
    """Should send first report immediately after bootstrap completes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)
        should_send = check_should_send_monthly_report(tmpdir)
        assert should_send


def test_no_report_immediately_after_sending():
    """Should not send another report immediately after sending one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)
        mark_report_sent(tmpdir, 5, 20)
        should_send = check_should_send_monthly_report(tmpdir)
        assert not should_send


def test_monthly_report_due_after_30_days():
    """Should send another report after 30 days have passed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)

        # Send first report
        mark_report_sent(tmpdir, 5, 20)

        # Artificially set last_report_sent_at to 31 days ago
        state_file = Path(tmpdir) / "bootstrap_report_state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))

        thirty_one_days_ago = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        state["last_report_sent_at"] = thirty_one_days_ago

        state_file.write_text(json.dumps(state), encoding="utf-8")

        # Should now be due
        should_send = check_should_send_monthly_report(tmpdir)
        assert should_send


def test_report_generation_with_no_cards():
    """Generate report when corpus has no cards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        report = generate_bootstrap_report(tmpdir, tmpdir)

        assert report["subject"] == "QAR Corpus Monthly Summary"
        assert "QAR Corpus Monthly Summary" in report["body"]
        assert "No cards" in report["body"] or "not been indexed" in report["body"]
        assert "issues" in report.get("issues", []) or "recommendations" in report


def test_report_generation_with_cards():
    """Generate report with actual indexed cards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create mock cards
        for i in range(5):
            card = {
                "id": f"card_{i}",
                "name": f"Topic {i}",
                "summary": f"Summary for topic {i}",
                "files": [f"file_{j}.py" for j in range(3)],
            }
            Path(tmpdir, f"{i}.json").write_text(json.dumps(card), encoding="utf-8")

        report = generate_bootstrap_report(tmpdir, tmpdir)

        assert "5 topic cards" in report["body"]
        assert "15 files" in report["body"]
        assert "QAR Corpus Monthly Summary" in report["body"]


def test_corpus_stats_counting():
    """Corpus stats should correctly count cards and files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create 3 cards with 4 files each
        for i in range(3):
            card = {
                "id": f"card_{i}",
                "files": [f"file_{j}.py" for j in range(4)],
            }
            Path(tmpdir, f"{i}.json").write_text(json.dumps(card), encoding="utf-8")

        stats = get_corpus_stats(tmpdir)

        assert stats["card_count"] == 3
        assert stats["file_count"] == 12


def test_report_includes_change_comparison():
    """Report should show changes when there's a previous state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)

        # Create initial cards
        for i in range(5):
            card = {"id": f"card_{i}", "files": [f"file_{j}" for j in range(2)]}
            Path(tmpdir, f"{i}.json").write_text(json.dumps(card), encoding="utf-8")

        # Send first report (5 cards, 10 files)
        mark_report_sent(tmpdir, 5, 10)

        # Add 2 more cards (7 cards, 14 files total)
        for i in range(5, 7):
            card = {"id": f"card_{i}", "files": [f"file_{j}" for j in range(2)]}
            Path(tmpdir, f"{i}.json").write_text(json.dumps(card), encoding="utf-8")

        # Generate new report
        report = generate_bootstrap_report(tmpdir, tmpdir)

        # Should show changes
        assert "Changes Since Last Report" in report["body"] or "+2" in report["body"] or "8 topic cards" in report["body"]


def test_state_persistence():
    """Bootstrap state should persist across calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Mark completed
        mark_bootstrap_completed(tmpdir)

        # Verify it persisted
        assert check_should_send_monthly_report(tmpdir)

        # Mark sent
        mark_report_sent(tmpdir, 10, 50)

        # In a "new" session, state should still be there
        assert not check_should_send_monthly_report(tmpdir)


def test_report_with_large_corpus():
    """Report should handle large corpus warnings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create many mock cards
        for i in range(150):
            card = {"id": f"card_{i}", "files": [f"file_{j}" for j in range(2)]}
            Path(tmpdir, f"{i}.json").write_text(json.dumps(card), encoding="utf-8")

        report = generate_bootstrap_report(tmpdir, tmpdir)

        # Should mention high card count
        assert "150" in report["body"] or "Recommendations" in report["body"]


def test_send_report_fails_without_bootstrap():
    """Should not send if bootstrap hasn't completed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        def mock_quest_factory():
            class MockClient:
                def send_email(self, **kwargs):
                    pass
            return MockClient()

        result = send_bootstrap_report_via_quest(
            tmpdir, tmpdir, "user@example.com", mock_quest_factory
        )
        assert not result


def test_send_report_succeeds_after_bootstrap():
    """Should send after bootstrap and mark state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)

        emails_sent = []

        def mock_quest_factory():
            class MockClient:
                def send_email(self, **kwargs):
                    emails_sent.append(kwargs)
            return MockClient()

        result = send_bootstrap_report_via_quest(
            tmpdir, tmpdir, "user@example.com", mock_quest_factory
        )

        assert result
        assert len(emails_sent) == 1
        assert "QAR Corpus Monthly Summary" in emails_sent[0]["subject"]
        assert "user@example.com" in emails_sent[0]["to"]


def test_send_report_not_due_within_30_days():
    """Should not resend if within 30 days."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mark_bootstrap_completed(tmpdir)
        mark_report_sent(tmpdir, 5, 20)

        def mock_quest_factory():
            class MockClient:
                def send_email(self, **kwargs):
                    raise AssertionError("Should not send email")
            return MockClient()

        result = send_bootstrap_report_via_quest(
            tmpdir, tmpdir, "user@example.com", mock_quest_factory
        )

        assert not result
