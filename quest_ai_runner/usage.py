"""Daily token usage tracker — prevents runaway API costs from shallow orchestrator calls.

Counts input + output tokens across ALL LLM provider calls (planning, answering, context indexing)
within a UTC day, persists the tally to a JSON file, and signals the poller to pause new task
pickup when the daily limit is exceeded. The counter resets automatically at midnight UTC.

The deep-runner (Claude Code, run via subscription) does NOT count toward this limit.

Env vars:
  QAR_DAILY_TOKEN_LIMIT   — daily cap on tokens (input + output, combined); no default (opt-in).
                            Set to a positive integer to enable. Set to 0 or "off" to disable.
  QAR_DAILY_USAGE_PATH    — JSON file for persisting today's count across restarts
                            (default: ./qar_daily_usage.json; gitignored by this repo).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger("quest-ai-runner.usage")


DEFAULT_DAILY_TOKEN_LIMIT = 2_000_000  # 2M tokens/day when nothing is configured


@dataclass
class DailyUsageLimits:
    """Daily token budget. ``None`` = disabled (no cap enforced)."""

    max_daily_tokens: Optional[int] = DEFAULT_DAILY_TOKEN_LIMIT

    @classmethod
    def from_env(cls, env=None) -> "DailyUsageLimits":
        """Read ``QAR_DAILY_TOKEN_LIMIT`` from env.

        Unset → default 2,000,000 tokens/day (protects against runaway costs).
        Set to 0 / "off" / "none" → disabled entirely.
        Set to a positive integer → that many tokens per day.
        """
        env = env or os.environ
        raw = (env.get("QAR_DAILY_TOKEN_LIMIT") or "").strip().lower()
        if not raw:
            return cls()  # default: 2M tokens/day
        if raw in ("0", "off", "none", "false", "disabled"):
            return cls(max_daily_tokens=None)
        try:
            limit = int(raw)
            return cls(max_daily_tokens=limit if limit > 0 else None)
        except ValueError:
            log.warning(
                "ignoring QAR_DAILY_TOKEN_LIMIT=%r — not an integer; using default %s",
                raw, f"{DEFAULT_DAILY_TOKEN_LIMIT:,}",
            )
            return cls()

    def enabled(self) -> bool:
        return self.max_daily_tokens is not None


class DailyUsageTracker:
    """Tracks today's token usage and checks against a daily limit.

    Thread-safe. The file is written after every :meth:`record` call so restarts resume from
    the correct total. At midnight UTC the counter resets automatically on the next check.
    """

    def __init__(self, path: Optional[str] = None, limits: Optional[DailyUsageLimits] = None):
        self._path = Path(path) if path else None
        self._limits = limits or DailyUsageLimits()
        self._lock = threading.Lock()
        self._date: str = ""
        self._tokens_in: int = 0
        self._tokens_out: int = 0
        self._load()

        if self._limits.enabled():
            log.info(
                "daily token limit: %s tokens/day; usage file: %s",
                f"{self._limits.max_daily_tokens:,}",
                self._path or "(in-memory only)",
            )

    @classmethod
    def from_env(cls, env=None) -> "DailyUsageTracker":
        """Build from ``QAR_DAILY_TOKEN_LIMIT`` / ``QAR_DAILY_USAGE_PATH`` env vars."""
        env = env or os.environ
        path = (env.get("QAR_DAILY_USAGE_PATH") or "").strip() or "./qar_daily_usage.json"
        limits = DailyUsageLimits.from_env(env)
        return cls(path=path, limits=limits)

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _load(self) -> None:
        today = self._today()
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if data.get("date") == today:
                    self._date = today
                    self._tokens_in = max(0, int(data.get("tokens_in", 0)))
                    self._tokens_out = max(0, int(data.get("tokens_out", 0)))
                    return
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                log.debug("daily usage file unreadable; starting fresh for today")
        self._date = today

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "date": self._date,
                "tokens_in": self._tokens_in,
                "tokens_out": self._tokens_out,
            }, indent=2)
            # Atomic write: temp file + os.replace(), so a crash/interruption mid-write can never
            # leave a corrupt/partial usage file behind (the replace is a single fs operation).
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(payload)
            os.replace(tmp_path, self._path)
        except OSError as e:
            log.warning("could not save daily usage file: %s", e)

    def _reset_if_new_day(self) -> None:
        today = self._today()
        if self._date != today:
            log.info(
                "new UTC day: resetting daily token counter "
                "(was %s tokens on %s; limit: %s)",
                f"{self._tokens_in + self._tokens_out:,}",
                self._date,
                f"{self._limits.max_daily_tokens:,}" if self._limits.enabled() else "disabled",
            )
            self._date = today
            self._tokens_in = 0
            self._tokens_out = 0

    def record(self, tokens_in: int, tokens_out: int) -> None:
        """Add tokens from one LLM call to today's running total."""
        with self._lock:
            self._reset_if_new_day()
            self._tokens_in += max(0, tokens_in)
            self._tokens_out += max(0, tokens_out)
            self._save()

    def total_tokens(self) -> int:
        """Total tokens used today (input + output)."""
        with self._lock:
            self._reset_if_new_day()
            return self._tokens_in + self._tokens_out

    def over_limit(self) -> bool:
        """True when the daily limit is enabled and has been reached or exceeded."""
        if not self._limits.enabled():
            return False
        return self.total_tokens() >= self._limits.max_daily_tokens  # type: ignore[operator]

    def status(self) -> str:
        """Human-readable summary for logs: ``"1,234/2,000,000 tokens today (0%)"``."""
        total = self.total_tokens()
        limit = self._limits.max_daily_tokens
        if limit is None:
            return f"{total:,} tokens today (no limit)"
        pct = 100.0 * total / limit if limit > 0 else 100.0
        return f"{total:,}/{limit:,} tokens today ({pct:.0f}%)"
