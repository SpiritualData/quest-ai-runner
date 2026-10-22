"""Deep-run dashboard: detail panel, run cycling, output flushing and the future-context toggle.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence
from collections import Counter, OrderedDict
from rich.text import Text

from .textual_ui_helpers import metric_key, metric_lines

# The key standing for the "no metric was recorded" line in ``metrics_shown``.
METRIC_MISSING_KEY = ("no metric recorded",)


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
            runs_this_turn = len(self._deep._runs)
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
        summary, narration = self._summarize_exec_lines(lines)

        log.write(Text(""))
        # 1) The subgoal this task was assigned, but only when it tells runs apart: with one run
        # it is the person's own request again, already on screen above.
        if runs_this_turn > 1:
            log.write(Text(f"⎅ {goal}" if goal else "⎅ Task", style="bold cyan"))
        # 2) A one-line roll-up of activity, instead of every read/write/command.
        if summary:
            log.write(Text(f"  {summary}", style="dim"))
        if final_output:
            # The result is the AI's reply, so it is introduced by its speaker like any answer.
            self.write_speaker_label()
            self._console.markdown(final_output)
        elif narration:
            # No structured result (e.g. an errored/incomplete run): fall back to the worker's own
            # words so the record still says what it was doing, capped so it never becomes a wall.
            tail = narration[-6:]
            if len(narration) > len(tail):
                log.write(Text(f"  … {len(narration) - len(tail)} earlier steps", style="dim"))
            for nl in tail:
                log.write(Text(f"  {nl}"))
        # What the metric was before and after, written from the run's own data (never by a model), as
        # its closing detail, even when no explanation is on.
        self.write_metrics(snap.get("metrics"), snap.get("metric_missing"))
        # No "complete" line here: a run's record does not know how the turn ended (met, stopped,
        # paused, waiting). The turn's one closing line says it, from the final result.

    def write_metrics(self, entries: Optional[Sequence[Mapping[str, Any]]], missing: Any = False) -> None:
        """Write the metric lines for ``entries`` that this turn's transcript does not hold yet.

        The same entry reaches the terminal more than once (a run's milestone, the turn's result, the
        explanation), and each is written once: an entry already written is skipped, however it arrived.
        ``missing`` adds the one line saying a change ended with no metric result recorded."""
        remaining = Counter(self.metrics_shown)
        fresh: List[Mapping[str, Any]] = []
        for entry in entries if isinstance(entries, (list, tuple)) else []:
            if not isinstance(entry, dict):
                continue
            key = metric_key(entry)
            if remaining[key] > 0:
                remaining[key] -= 1
                continue
            fresh.append(entry)
            self.metrics_shown[key] += 1
        absent = missing is True and remaining[METRIC_MISSING_KEY] == 0
        if absent:
            self.metrics_shown[METRIC_MISSING_KEY] += 1
        for line in metric_lines(fresh, absent, self.transcript_width()):
            self._tlog.write(line)

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
        plain 'c' would be typed into the message instead of toggling. Opens whatever the last turn
        loaded into the panel (a deep run's context, or the cards a short answer used). Pressing
        Alt+C again closes it; when nothing was loaded the action is a no-op.
        """
        if self._future_ctx_panel.display:
            self._future_ctx_panel.display = False
        elif self._future_ctx_panel.has_bullets:
            self._future_ctx_panel._rerender()
            self._future_ctx_panel.display = True
