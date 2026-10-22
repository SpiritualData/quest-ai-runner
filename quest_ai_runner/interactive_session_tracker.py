"""The deep-run tracker: per-run goals, output, status and the dashboard text.

Split out of ``interactive_session.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


def format_elapsed(seconds: float) -> str:
    """``45s`` under a minute, ``2m5s`` after: the one way a run's elapsed time is written."""
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m{secs}s" if mins else f"{secs}s"


class _DeepRunTracker:
    """Track multiple concurrent deep runs and their latest output.

    When multiple deep tasks execute concurrently, shows a dashboard with latest
    output from each, and allows toggling to view one run's full progress.
    """

    def __init__(self) -> None:
        self._runs: dict = {}  # run_id -> {'goal': str, 'status': str, 'output': str, 'started': float}
        self._lock = threading.Lock()
        self._active_run_id: Optional[str] = None  # which run is currently displayed in detail

    def add_run(self, run_id: str, goal: str) -> None:
        """Register a new deep run."""
        with self._lock:
            self._runs[run_id] = {
                'goal': goal,
                'status': 'running',
                'output': '',
                'started': time.time(),
                'exec_lines': [],  # accumulate exec events for this run
                'final_output': '',  # the worker's final result, set on completion
                'model': None,  # the LLM model this run actually executed with, set on completion
                'metrics': [],  # the run's metric results (payload entries), set on completion
                'metric_missing': False,  # a change that ended with no metric result recorded
            }
            if self._active_run_id is None:
                self._active_run_id = run_id

    def set_final_output(self, run_id: str, text: str) -> None:
        """Record a deep run's final worker output (its result, not a progress tick)."""
        with self._lock:
            if run_id in self._runs and text:
                self._runs[run_id]['final_output'] = text

    def set_metrics(self, run_id: str, metrics: List[dict], missing: bool = False) -> None:
        """Record a deep run's metric results (before and after, by code) and whether a change had none."""
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]['metrics'] = list(metrics or [])
                self._runs[run_id]['metric_missing'] = bool(missing)

    def set_model(self, run_id: str, model: str) -> None:
        """Record which LLM model a deep run actually executed with."""
        with self._lock:
            if run_id in self._runs and model:
                self._runs[run_id]['model'] = model

    def update_goal(self, run_id: str, goal: str) -> None:
        """Set/refine a run's goal text once the real subgoal is known."""
        with self._lock:
            if run_id in self._runs and goal:
                self._runs[run_id]['goal'] = goal

    def update_run_output(self, run_id: str, text: str, record: bool = True) -> None:
        """Add output to a deep run's progress.

        ``record=False`` is for a line that is only a sign of life (the worker's heartbeat): the live
        dashboard shows it, but it is not one of the run's actions, so it never reaches the run's
        record, the Alt+D replay or the activity roll-up."""
        with self._lock:
            if run_id in self._runs:
                # Keep latest 10 lines of output for dashboard view
                current = self._runs[run_id]['output']
                lines = (current + '\n' + text).strip().split('\n')
                self._runs[run_id]['output'] = '\n'.join(lines[-10:])
                if record:
                    self._runs[run_id]['exec_lines'].append(text)

    def set_run_status(self, run_id: str, status: str) -> None:
        """Update a run's status (running/done/error)."""
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]['status'] = status

    def get_dashboard(self, lines_per_run: Optional[int] = None,
                      active_run_id: Optional[str] = None) -> str:
        """Return a dashboard summary of all runs with latest output.

        ``lines_per_run`` controls how many output lines are shown per agent.
        When omitted it scales automatically: 5 for one run, 3 for two, 2 for three+.

        ``active_run_id`` (optional), when it names one of the runs, marks that run's header line
        as the one Alt+D/Tab/a click would currently open, so a user with several concurrent runs
        can see which one is selected without having to open the detail panel first.
        """
        text, _ = self._render_dashboard(lines_per_run, active_run_id)
        return text

    def get_dashboard_with_map(self, lines_per_run: Optional[int] = None,
                               active_run_id: Optional[str] = None) -> "Tuple[str, Dict[int, str]]":
        """Same as ``get_dashboard``, plus a ``{row_index: run_id}`` map (0-based, matching the
        returned text's lines) so a UI can hit-test a click's row against the run it fell within
        (click-to-expand)."""
        return self._render_dashboard(lines_per_run, active_run_id)

    def _render_dashboard(self, lines_per_run: Optional[int],
                          active_run_id: Optional[str]) -> "Tuple[str, Dict[int, str]]":
        with self._lock:
            if not self._runs:
                return "", {}

            if lines_per_run is None:
                # Scale to keep the inline block calm but actually legible: a
                # single run gets a few lines (the common case the user reads),
                # concurrent runs tighten so the block doesn't balloon.
                n = len(self._runs)
                lines_per_run = 3 if n <= 1 else (2 if n == 2 else 1)

            lines: List[str] = []
            row_run: Dict[int, str] = {}
            for run_id, info in sorted(self._runs.items()):
                block_start = len(lines)
                status_icon = "●" if info['status'] == 'running' else ("✓" if info['status'] == 'done' else "✗")
                time_str = format_elapsed(time.time() - info['started'])

                # The SUBGOAL this run is working on: its own prominent (bold cyan) header line so
                # the user always sees WHAT the live actions below are for. Shown fully (generous cap
                # vs the old 60 chars that cut sentences mid-word); the renderer wraps if needed.
                # One expand/collapse arrow per run, on this header line only (the status/elapsed
                # line below carries no arrow of its own, so there's never a second one to confuse
                # with this one): "▾" when this run is the one currently open in the detail panel
                # (what Alt+D/Tab/a click would open right now), "▸" otherwise, mirroring the
                # familiar expanded/collapsed chevron convention. The open run also gets a brighter
                # (bold yellow) header instead of plain bold cyan, so it's visible at a glance.
                goal = " ".join((info['goal'] or "").split())
                if len(goal) > 160:
                    goal = goal[:160].rstrip() + "…"
                goal_text = goal or "Deep task"
                if run_id == active_run_id:
                    lines.append(f"\x1b[1;33m▾ ⎅ {goal_text}\x1b[0m")
                else:
                    lines.append(f"\x1b[1;36m▸ ⎅ {goal_text}\x1b[0m")
                # Status + elapsed sit under the subgoal, then the latest live action lines.
                lines.append(f"\x1b[2m  {status_icon} {time_str}\x1b[0m")

                if info['output']:
                    output_lines = [l.strip() for l in info['output'].split('\n') if l.strip()]
                    for ol in output_lines[-lines_per_run:]:
                        prefix = "  → " if '/' in ol else "    "
                        lines.append(f"{prefix}{ol}")

                for row in range(block_start, len(lines)):
                    row_run[row] = run_id

            return "\n".join(lines), row_run

    def set_active_run(self, run_id: str) -> bool:
        """Switch to viewing a specific run's detailed progress."""
        with self._lock:
            if run_id in self._runs:
                self._active_run_id = run_id
                return True
        return False

    def next_run(self) -> Optional[str]:
        """Cycle to the next run."""
        with self._lock:
            run_ids = sorted(self._runs.keys())
            if not run_ids:
                return None
            if self._active_run_id is None or self._active_run_id not in run_ids:
                self._active_run_id = run_ids[0]
                return self._active_run_id
            idx = run_ids.index(self._active_run_id)
            self._active_run_id = run_ids[(idx + 1) % len(run_ids)]
            return self._active_run_id

    def get_active_run(self) -> Optional[str]:
        """Get the currently active run ID."""
        with self._lock:
            return self._active_run_id

    def get_run_detail(self, run_id: Optional[str]) -> str:
        """Get full execution details for a run."""
        with self._lock:
            if run_id and run_id in self._runs:
                info = self._runs[run_id]
                return f"\n".join(info.get('exec_lines', [])[-50:])  # last 50 exec events
        return ""
