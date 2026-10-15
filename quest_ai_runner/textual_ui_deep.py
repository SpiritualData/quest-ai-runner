"""Deep-run dashboard: detail panel, run cycling, output flushing and the future-context toggle.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import time
from typing import List
from collections import OrderedDict
from rich.text import Text


class DeepMixin:
    """Deep-run dashboard: detail panel, run cycling, output flushing and the future-context toggle."""

    def _available_deep_runs(self) -> "OrderedDict[str, dict]":
        """Runs the Alt+D detail panel can open, in chronological order.

        While a turn has live/just-finished runs they win (the live tracker, not yet rebuilt for the
        next turn); otherwise we fall back to the cross-turn archive of finished runs, so Alt+D still
        works after the run is done and even after later (non-deep) turns.
        """
        with self._deep._lock:
            live = OrderedDict(self._deep._runs)
        if live:
            return live
        return OrderedDict(self._deep_archive)

    def _open_detail_for(self, run_id: str) -> None:
        """Open the detail panel for a specific run_id, computing pos/total."""
        runs = self._available_deep_runs()
        info = runs.get(run_id)
        if info is None:
            return
        run_ids = sorted(runs.keys())
        pos = run_ids.index(run_id) + 1 if run_id in run_ids else 1
        total = len(run_ids)
        existing = list(info.get("exec_lines", []))
        self._deep_detail.open_for(run_id, info["goal"], existing, pos=pos, total=total)
        self._refresh_deep_dashboard()

    def _refresh_deep_dashboard(self) -> None:
        """Redraw the inline dashboard so its expand/collapse arrow (▾/▸) tracks which run, if
        any, is actually open in the detail panel right now.

        Called right after the detail panel opens or closes (click, Alt+D, Tab), not just on the
        next throttled exec-event redraw, so the arrow flips the instant the panel does instead of
        lagging behind (or during a quiet run, never catching up at all).
        """
        if not self._deep._runs:
            return
        n = len(self._deep._runs)
        dashboard, line_map = self._deep.get_dashboard_with_map(active_run_id=self._deep_detail.active_run_id)
        self._deep_view.show(dashboard, n_runs=n, line_map=line_map)

    @staticmethod
    def _summarize_exec_lines(lines: List[str]) -> tuple:
        """Condense a deep run's captured exec lines into (activity_summary, narration).

        The scrollback record is a SUMMARY, not a replay: instead of one line per read/write/command
        (a wall the user doesn't want), we roll the mechanical tool actions up into a single counts
        line ("12 reads · 3 edits · 2 commands") and return the worker's own narration separately so
        a run with no structured result can still show what it was doing in its own words.

        CONSECUTIVE IDENTICAL narration lines collapse into one, tagged with how many times it
        repeated. A goal loop that retries the same subgoal re-reports under the same run_id, so a
        failure that is identical every attempt (e.g. a model the worker cannot run, which errors
        the same way six times) otherwise fills the record with the same sentence six times and
        crowds out everything else the run said. Show the real thing once, and say it repeated.
        """
        reads = writes = cmds = searches = tools = 0
        narration: List[str] = []
        repeats: List[int] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("Read:"):
                reads += 1
            elif s.startswith(("Write:", "Edit:")):
                writes += 1
            elif s.startswith("$ "):
                cmds += 1
            elif s.startswith(("WebSearch:", "WebFetch:")):
                searches += 1
            elif s.startswith("Using "):
                tools += 1
            elif s.startswith("[thinking]"):
                continue  # internal reasoning — not part of the "what it did" summary
            elif narration and narration[-1] == s:
                repeats[-1] += 1  # the same line again: count it, don't print it again
            else:
                narration.append(s)
                repeats.append(1)

        def _plural(n: int, one: str, many: str) -> str:
            return f"{n} {one if n == 1 else many}"

        parts: List[str] = []
        if reads:
            parts.append(_plural(reads, "read", "reads"))
        if writes:
            parts.append(_plural(writes, "edit", "edits"))
        if cmds:
            parts.append(_plural(cmds, "command", "commands"))
        if searches:
            parts.append(_plural(searches, "search", "searches"))
        if tools:
            parts.append(_plural(tools, "tool call", "tool calls"))
        narrated = [n if c == 1 else f"{n} (repeated {c} times)"
                    for n, c in zip(narration, repeats)]
        return " · ".join(parts), narrated

    def _flush_deep_run(self, run_id: str) -> None:
        """Write one deep run's record into the scrollback transcript, once.

        The live deep widgets are ephemeral (hidden when the turn ends), so without this a finished
        task's output would vanish. We write, exactly once per run (tracked in ``_deep_flushed``), a
        compact SUMMARY (not a replay): the SUBGOAL it was assigned (header), a one-line activity
        roll-up of what it touched, and — most important — its FINAL OUTPUT (the worker's own
        summary of what it did). The full step-by-step trace stays available live and in the detail
        panel (Alt+D); the permanent record stays readable.
        """
        if run_id in self._deep_flushed:
            return
        with self._deep._lock:
            info = self._deep._runs.get(run_id)
            snap = dict(info) if info else None
        if not snap:
            return
        lines = [l.strip() for l in (snap.get("exec_lines") or []) if l and l.strip()]
        final_output = (snap.get("final_output") or "").strip()
        # Mark flushed regardless so we never reconsider this run; skip writing an
        # empty block when nothing at all was captured.
        self._deep_flushed.add(run_id)
        if not lines and not final_output:
            return
        # Archive this finished run (with its full captured actions) so Alt+D can replay it later,
        # even after the live tracker is rebuilt for the next turn. Only runs with actual actions are
        # worth keeping (the detail panel shows the step trace); cap to the most recent.
        if lines:
            archived = dict(snap)
            archived["exec_lines"] = list(lines)
            self._deep_archive[run_id] = archived
            while len(self._deep_archive) > self._DEEP_ARCHIVE_MAX:
                self._deep_archive.popitem(last=False)
        log = self._tlog
        goal = (snap.get("goal") or "").strip()
        status = snap.get("status") or "running"
        elapsed = time.time() - snap.get("started", time.time())
        mins, secs = divmod(int(elapsed), 60)
        time_str = f"{mins}m{secs}s" if mins else f"{secs}s"
        summary, narration = self._summarize_exec_lines(lines)

        pass_num = self._auto_pass
        pass_label = f" [Pass {pass_num}]"

        log.write(Text(""))
        # 1) The subgoal this task was assigned — the most important orientation.
        log.write(Text(f"⎅{pass_label} {goal}" if goal else f"⎅{pass_label} Deep task", style="bold cyan"))
        # 2) A one-line roll-up of activity, instead of every read/write/command.
        if summary:
            log.write(Text(f"  {summary}", style="dim"))
        if final_output:
            log.write(Text(""))
            log.write(Text("  Result", style="bold green"))
            self._console.markdown(final_output)
        elif narration:
            # No structured result (e.g. an errored/incomplete run): fall back to the worker's own
            # words so the record still says what it was doing, capped so it never becomes a wall.
            tail = narration[-6:]
            if len(narration) > len(tail):
                log.write(Text(f"  … {len(narration) - len(tail)} earlier steps", style="dim"))
            for nl in tail:
                log.write(Text(f"  {nl}"))
        model = (snap.get("model") or "").strip()
        model_suffix = f" · {model}" if model else ""
        if status == "error":
            log.write(Text(f"  ✗ Deep task ended with an error · {time_str}{model_suffix}", style="red"))
        else:
            log.write(Text(f"  ✓ Deep task complete · {time_str}{model_suffix}", style="green"))
        # Point at the full per-action trace (the summary above is a roll-up). Only when there are
        # actions to replay; the panel stays available after the turn via the archive.
        if lines:
            log.write(Text("  Alt+D: see every action", style="dim"))
        log.write(Text(""))

    def _flush_pending_deep_runs(self) -> None:
        """Flush any deep runs that didn't already emit a terminal phase.

        Covers runs with no explicit done/error tick and the cancel/error paths.
        """
        with self._deep._lock:
            run_ids = sorted(self._deep._runs.keys())
        for rid in run_ids:
            self._flush_deep_run(rid)

    def action_toggle_deep_detail(self) -> None:
        """Toggle the expanded detail view for the current deep run (key: Alt+D).

        Alt+D (not a bare 'd') because the prompt Input consumes printable keys, so a plain 'd'
        would be typed into the message instead of toggling. Alt+D is not consumed and fires reliably.
        """
        if self._deep_detail.display:
            self._deep_detail.hide()
            self._refresh_deep_dashboard()
            return
        runs = self._available_deep_runs()
        if not runs:
            return
        # Prefer the run currently/last streaming this turn; otherwise open the most recent finished
        # one (last inserted), so Alt+D shows a deep task's actions even after it's done.
        run_id = self._cur_deep_run or self._deep.get_active_run()
        if run_id not in runs:
            run_id = next(reversed(runs))
        self._open_detail_for(run_id)

    def action_cycle_deep_run(self) -> None:
        """Cycle the detail panel to the next deep run (key: Tab).

        Cycles over whatever runs are available — the live tracker during a turn, or the finished-run
        archive afterwards — keyed off the panel's currently-shown run so it works post-turn too.
        While the "/" command menu is open, Tab completes the highlighted command instead.
        """
        if self.command_menu_key("tab"):
            return
        runs = self._available_deep_runs()
        if not runs:
            return
        run_ids = sorted(runs.keys())
        cur = self._deep_detail.active_run_id
        if cur in run_ids:
            run_id = run_ids[(run_ids.index(cur) + 1) % len(run_ids)]
        else:
            run_id = run_ids[0]
        self._open_detail_for(run_id)

    def action_toggle_future_context(self) -> None:
        """Toggle the "context it used" panel (key: Alt+C).

        Alt+C is used (not a bare letter) because the prompt Input consumes printable keys, so a
        plain 'c' would be typed into the message instead of toggling. Only opens when there is
        non-empty context from the most recent deep run. Pressing Alt+C again closes it; when there
        is nothing to show the action is a no-op.
        """
        if self._future_ctx_panel.display:
            self._future_ctx_panel.display = False
        elif self._future_context:
            self._future_ctx_panel._rerender()
            self._future_ctx_panel.display = True
