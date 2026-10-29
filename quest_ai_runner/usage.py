"""Daily token usage tracker — prevents runaway API costs from shallow orchestrator calls.

Counts input + output tokens across ALL LLM provider calls (planning, answering, context indexing)
within a UTC day, persists the tally to a JSON file, and signals the poller to pause new task
pickup when the daily limit is exceeded. The counter resets automatically at midnight UTC.

The deep-runner (Claude Code, run via subscription) does NOT count toward this limit.

Per-call records: every call is also kept as a ``CallRecord`` tagged with the id of the registered prompt spec that
wrote its prompt (``PromptSpec.render`` sets the tag, ``active_spec`` reads it), in ``SpendLog``. ``SpendLog.summary``
gives each prompt's fire rate per turn and its average and maximum tokens, dollars and seconds per call, the
measurement behind ``metric-justified-model-spend``. A call made with no spec in scope is tagged ``UNTAGGED``.

Env vars:
  QAR_DAILY_TOKEN_LIMIT   — daily cap on tokens (input + output, combined); no default (opt-in).
                            Set to a positive integer to enable. Set to 0 or "off" to disable.
  QAR_DAILY_USAGE_PATH    — JSON file for persisting today's count across restarts
                            (default: ./qar_daily_usage.json; gitignored by this repo).
  QAR_CALL_LOG_PATH       — JSON-lines file with one line per model call (time, prompt spec id, tokens, dollars, seconds),
                            the production record that ``summarize_call_log`` reads (default: the usage file's name with
                            ``_calls.jsonl``; "off" disables it). Rotated to ``.1`` past ``CALL_LOG_MAX_BYTES``.
"""
from __future__ import annotations

import json
import logging
import os
import contextvars
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger("quest-ai-runner.usage")


DEFAULT_DAILY_TOKEN_LIMIT = 2_000_000  # 2M tokens/day when nothing is configured

UNTAGGED = "(untagged)"   # the tag of a call made while no registered prompt spec is in scope

_ACTIVE_SPEC: "contextvars.ContextVar[str]" = contextvars.ContextVar("qar_active_prompt_spec", default="")


def tag_spec(spec_id: str) -> None:
    """Mark ``spec_id`` as the prompt whose model call comes next in this context (set by ``PromptSpec.render``)."""
    _ACTIVE_SPEC.set(spec_id or "")


def active_spec() -> str:
    """The id of the registered prompt spec last rendered in this context, or ``UNTAGGED``."""
    return _ACTIVE_SPEC.get() or UNTAGGED


@dataclass(frozen=True)
class CallRecord:
    """One model call as the usage records keep it."""

    spec_id: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    seconds: float
    turn: int
    critical_path: bool = True   # the reply waits on the call


class SpendLog:
    """Per-call records tagged with the prompt spec id, and the per-prompt statistics read from them. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list = []
        self.turns = 0

    def begin_turn(self) -> int:
        """Count one turn (the denominator of every fire rate) and return its number."""
        with self._lock:
            self.turns += 1
            return self.turns

    def record(self, spec_id: str, tokens_in: int, tokens_out: int, cost_usd: float = 0.0, seconds: float = 0.0,
               critical_path: bool = True) -> CallRecord:
        with self._lock:
            call = CallRecord(spec_id or UNTAGGED, max(0, int(tokens_in)), max(0, int(tokens_out)), max(0.0, float(cost_usd)),
                              max(0.0, float(seconds)), self.turns, critical_path)
            self.records.append(call)
            return call

    def summary(self) -> dict:
        """``{spec_id: stats}``: calls, turns it fired in, fire rate per turn, average and maximum of tokens in and out,
        dollars and seconds per call, the share of its calls on the critical path, and total dollars."""
        with self._lock:
            records, turns = list(self.records), self.turns
        out: dict = {}
        for spec_id in sorted({r.spec_id for r in records}):
            mine = [r for r in records if r.spec_id == spec_id]
            fired = len({r.turn for r in mine})

            def stat(field: str) -> dict:
                values = [getattr(r, field) for r in mine]
                return {"avg": sum(values) / len(values), "max": max(values)}
            out[spec_id] = {
                "calls": len(mine), "turns_fired": fired, "fire_rate": fired / turns if turns else 0.0,
                "tokens_in": stat("tokens_in"), "tokens_out": stat("tokens_out"),
                "cost_usd": stat("cost_usd"), "seconds": stat("seconds"),
                "critical_path_share": sum(1 for r in mine if r.critical_path) / len(mine),
                "total_cost_usd": sum(r.cost_usd for r in mine)}
        return out


CALL_LOG_MAX_BYTES = 5_000_000


class CallLog:
    """Append-only JSON-lines record of every model call, tagged with the prompt spec id, so a metric can read production
    per-call seconds, tokens and dollars after the process is gone. Thread-safe; a write failure is logged, never raised."""

    def __init__(self, path: Optional[str], max_bytes: int = CALL_LOG_MAX_BYTES) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, spec_id: str, tokens_in: int, tokens_out: int, cost_usd: float, seconds: float, now: Optional[float] = None) -> None:
        if self.path is None:
            return
        import time
        line = json.dumps({"ts": round(time.time() if now is None else now, 3), "spec_id": spec_id or UNTAGGED,
                           "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
                           "cost_usd": round(float(cost_usd), 8), "seconds": round(float(seconds), 3)}, sort_keys=True)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as e:
            log.warning("could not append to the call log: %s", e)


def read_call_log(path) -> list:
    """The call records in a call log file (and its rotated ``.1`` file, older first) as dicts; unreadable lines are skipped."""
    records: list = []
    for file in (Path(str(path) + ".1"), Path(path)):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "spec_id" in row:
                records.append(row)
    return records


def summarize_call_log(path, spec_id: Optional[str] = None) -> dict:
    """``{spec_id: {"calls", "seconds": {"avg","max"}, "tokens_in": {...}, "tokens_out": {...}, "cost_usd": {...}}}`` from a call log;
    ``spec_id`` narrows it to one prompt. An empty dict means the log holds no such call (nothing is guessed)."""
    out: dict = {}
    rows = read_call_log(path)
    for sid in sorted({r["spec_id"] for r in rows}):
        if spec_id and sid != spec_id:
            continue
        mine = [r for r in rows if r["spec_id"] == sid]

        def stat(field: str) -> dict:
            values = [float(r.get(field) or 0) for r in mine]
            return {"avg": sum(values) / len(values), "max": max(values)}
        out[sid] = {"calls": len(mine), **{f: stat(f) for f in ("seconds", "tokens_in", "tokens_out", "cost_usd")}}
    return out


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

    def __init__(self, path: Optional[str] = None, limits: Optional[DailyUsageLimits] = None,
                 call_log_path: Optional[str] = None):
        self._path = Path(path) if path else None
        self._limits = limits or DailyUsageLimits()
        self._lock = threading.Lock()
        self._date: str = ""
        self._tokens_in: int = 0
        self._tokens_out: int = 0
        self.spend_log = SpendLog()
        self.call_log = CallLog(call_log_path)
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
        raw = (env.get("QAR_CALL_LOG_PATH") or "").strip()
        if raw.lower() == "off":
            log_path = None
        else:
            log_path = raw or str(Path(path).with_suffix("")) + "_calls.jsonl"
        return cls(path=path, limits=limits, call_log_path=log_path)

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

    def record(self, tokens_in: int, tokens_out: int, spec_id: Optional[str] = None, cost_usd: float = 0.0,
               seconds: float = 0.0) -> None:
        """Add tokens from one LLM call to today's running total, and keep the call in ``spend_log`` tagged ``spec_id``."""
        self.spend_log.record(spec_id or UNTAGGED, tokens_in, tokens_out, cost_usd, seconds)
        self.call_log.append(spec_id or UNTAGGED, tokens_in, tokens_out, cost_usd, seconds)
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
