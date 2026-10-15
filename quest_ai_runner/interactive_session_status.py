"""Read-only status printers: whoami, tasks, status and daily usage.

A mixin of ``InteractiveSession`` (see ``interactive_session.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import os
from typing import Optional

from .interactive_session_render import _CYAN, _GOLD, _GREEN, _a


class StatusMixin:
    """Read-only status printers: whoami, tasks, status and daily usage."""

    def _print_whoami(self) -> None:
        c = self._console
        c.line("")
        c.speaker(self._rep_name, "cyan", "Session info")
        c.line("")
        if self._persona:
            kb = max(1, len(self._persona.encode()) // 1024)
            c.bullet(f"representative: {self._rep_name} ({kb}KB skill file)", indent=1)
        else:
            c.bullet(f"representative: {self._rep_name} (use /reps to load a skill file)", indent=1)
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            c.bullet(f"corpus: {corpus}", indent=1)
        if self._goal_id:
            c.bullet(f"goal: {self._goal_id}", indent=1)
        c.bullet(f"turns: {self._turn_count} in this session", indent=1)
        c.line("")

    def _print_tasks(self) -> None:
        """Show recently completed tasks with metadata."""
        c = self._console
        if not self._turns:
            c.line("")
            c.dim("  No turns yet.")
            c.line("")
            return

        c.line("")
        c.speaker("Tasks", "cyan", f"{len(self._turns)} completed")
        c.line("")

        # Show up to last 10 turns
        for i, turn in enumerate(self._turns[-10:], 1):
            user_text = turn["user"]
            model = turn["model"] or "?"
            tokens = f"{turn['tokens_in']}↥ {turn['tokens_out']}↦"
            elapsed = f"{turn['elapsed']:.1f}s"

            # Color the model tier
            model_colored = model
            if "haiku" in model.lower():
                model_colored = _a(_CYAN, model)
            elif "sonnet" in model.lower():
                model_colored = _a(_GREEN, model)
            elif "opus" in model.lower():
                model_colored = _a(_GOLD, model)

            c.dim(f"  {i:2d}.  {user_text[:45]}...")
            c.dim(f"       {model_colored}  {tokens}  {elapsed}")

        c.line("")

    def _print_status(self) -> None:
        """Show session statistics: token usage, model distribution, speed metrics."""
        c = self._console
        c.line("")
        c.speaker("Session stats", "cyan", "")
        c.line("")

        if not self._turns:
            c.bullet("turns: 0", indent=1)
            c.bullet("total tokens: 0", indent=1)
            c.line("")
            return

        # Aggregate stats
        total_in = sum(t["tokens_in"] for t in self._turns)
        total_out = sum(t["tokens_out"] for t in self._turns)
        avg_in = total_in // len(self._turns) if self._turns else 0
        avg_out = total_out // len(self._turns) if self._turns else 0
        total_time = sum(t["elapsed"] for t in self._turns)
        avg_time = total_time / len(self._turns) if self._turns else 0

        # Model distribution
        model_counts: dict = {}
        for t in self._turns:
            model = t["model"] or "unknown"
            model_counts[model] = model_counts.get(model, 0) + 1

        # Speed metrics
        times = [t["elapsed"] for t in self._turns]
        fastest = min(times) if times else 0
        slowest = max(times) if times else 0

        c.bullet(f"turns: {len(self._turns)}", indent=1)
        c.bullet(f"total tokens: {total_in + total_out:,} ({total_in:,} in, {total_out:,} out)",
                indent=1)
        c.bullet(f"avg tokens per turn: {avg_in} in, {avg_out} out", indent=1)
        c.line("")

        c.dim("  Models used:")
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            pct = (count / len(self._turns) * 100) if self._turns else 0
            c.dim(f"    {model:8s}  {count:2d} turn{'s' if count != 1 else ''}  ({pct:5.1f}%)")

        c.line("")
        c.dim(f"  Speed:  fastest {fastest:.1f}s  ·  slowest {slowest:.1f}s  ·  avg {avg_time:.1f}s")
        c.line("")

        usage_line = self._daily_usage_status()
        if usage_line:
            c.dim(f"  Daily budget:  {usage_line}")
            c.line("")

    def _daily_usage_status(self) -> Optional[str]:
        """Read-only daily-usage status line for /status, or None if no usage file exists.

        Instantiates a DailyUsageTracker from the same env path the poller uses
        (QAR_DAILY_USAGE_PATH, default ./qar_daily_usage.json) purely to read today's tally;
        this session never records against it (only the poller/orchestrator calls do that).
        """
        try:
            path = (os.getenv("QAR_DAILY_USAGE_PATH") or "").strip() or "./qar_daily_usage.json"
            if not os.path.exists(path):
                return None
            from .usage import DailyUsageTracker
            return DailyUsageTracker.from_env().status()
        except Exception:  # noqa: BLE001 — status display must never break /status
            return None
