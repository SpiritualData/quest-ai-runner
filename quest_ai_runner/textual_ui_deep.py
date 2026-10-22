"""Deep-run dashboard: detail panel, run cycling, output flushing and the future-context toggle.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence
from collections import Counter, OrderedDict
from rich.text import Text

from .textual_ui_helpers import METRIC_LINES_SHOWN, indented_line, metric_key, metric_lines, union_entries

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
        # Every metric result of the job (the transcript shows the first few): its own, else the turn's.
        metrics = info.get("metrics") or self.metrics_listing
        self._deep_detail.open_for(run_id, info["goal"], existing, pos=pos, total=total, metrics=metrics)
        self._refresh_deep_dashboard()

    def _refresh_deep_dashboard(self) -> None:
        """Redraw the inline dashboard so its expand/collapse arrow (▾/▸) tracks which run, if
        any, is actually open in the detail panel right now.

        Called right after the detail panel opens or closes (click, Alt+D, Tab), not just on the
        next throttled exec-event redraw, so the arrow flips the instant the panel does instead of
        lagging behind (or during a quiet run, never catching up at all).
        """
        if not self._deep._runs or not self._turn_active:
            # With the turn over the live panel is gone for good: opening a job's details (Alt+D) must not
            # bring it back above them.
            return
        n = len(self._deep._runs)
        dashboard, line_map = self._deep.get_dashboard_with_map(active_run_id=self._deep_detail.active_run_id)
        self._deep_view.show(dashboard, n_runs=n, line_map=line_map)

    @staticmethod
    def exec_line_kind(line: str) -> str:
        """What a captured exec line is: ``read``, ``edit``, ``command``, ``search``, ``tool``, ``thinking`` or
        ``narration`` (the worker's own words). The one place that decides, so a job's summary line and the
        turn's action count can never disagree."""
        if line.startswith("Read:"):
            return "read"
        if line.startswith(("Write:", "Edit:")):
            return "edit"
        if line.startswith("$ "):
            return "command"
        if line.startswith(("WebSearch:", "WebFetch:")):
            return "search"
        if line.startswith("Using "):
            return "tool"
        if line.startswith("[thinking]"):
            return "thinking"
        return "narration"

    def turn_action_count(self) -> int:
        """How many actions the jobs of this turn took, counted as their summary lines count them."""
        total = 0
        for run_id in self._deep_flushed:
            lines = (self._deep_archive.get(run_id) or {}).get("exec_lines") or []
            total += sum(1 for ln in lines if self.exec_line_kind(ln.strip()) not in ("thinking", "narration"))
        return total

    @staticmethod
    def _summarize_exec_lines(lines: List[str]) -> tuple:
        """Condense a deep run's captured exec lines into (activity_summary, narration).

        The scrollback record is a SUMMARY, not a replay: instead of one line per read/write/command
        (a wall the user doesn't want), we roll the mechanical tool actions up into a single counts
        line ("12 files read · 3 edits · 2 commands") and return the worker's own narration separately so
        a run with no structured result can still show what it was doing in its own words.

        CONSECUTIVE IDENTICAL narration lines collapse into one, tagged with how many times it
        repeated. A goal loop that retries the same subgoal re-reports under the same run_id, so a
        failure that is identical every attempt (e.g. a model the worker cannot run, which errors
        the same way six times) otherwise fills the record with the same sentence six times and
        crowds out everything else the run said. Show the real thing once, and say it repeated.
        """
        counts = {"read": 0, "edit": 0, "command": 0, "search": 0, "tool": 0}
        narration: List[str] = []
        repeats: List[int] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            kind = DeepMixin.exec_line_kind(s)
            if kind in counts:
                counts[kind] += 1
            elif kind == "thinking":
                continue  # internal reasoning — not part of the "what it did" summary
            elif narration and narration[-1] == s:
                repeats[-1] += 1  # the same line again: count it, don't print it again
            else:
                narration.append(s)
                repeats.append(1)

        def _plural(n: int, one: str, many: str) -> str:
            return f"{n} {one if n == 1 else many}"

        # "Files read", never "reads": "sources" is the word for what the AI looked at before it started.
        parts: List[str] = []
        if counts["read"]:
            parts.append(_plural(counts["read"], "file read", "files read"))
        if counts["edit"]:
            parts.append(_plural(counts["edit"], "edit", "edits"))
        if counts["command"]:
            parts.append(_plural(counts["command"], "command", "commands"))
        if counts["search"]:
            parts.append(_plural(counts["search"], "search", "searches"))
        if counts["tool"]:
            parts.append(_plural(counts["tool"], "tool call", "tool calls"))
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
            log.write(indented_line(goal or "Task", "bold cyan", hang=0))
        # 2) A one-line roll-up of activity, instead of every read/write/command.
        if summary:
            log.write(indented_line(f"  {summary}", "dim"))
        if final_output:
            # The result is the AI's reply, so it is introduced by its speaker like any answer.
            self.write_speaker_label()
            self._console.markdown(final_output)
        elif narration:
            # No structured result (e.g. an errored/incomplete run): fall back to the worker's own
            # words so the record still says what it was doing, capped so it never becomes a wall.
            tail = narration[-6:]
            if len(narration) > len(tail):
                log.write(indented_line(f"  … {len(narration) - len(tail)} earlier steps", "dim"))
            for nl in tail:
                log.write(indented_line(f"  {nl}"))
        # No metric lines and no "complete" line here: a run's record does not know what the turn says after
        # it (an answer of its own) or how it ended (met, stopped, paused, waiting). The metric block and the
        # one closing line belong to the turn's end (``write_turn_metrics``, ``_write_footer``).

    def write_turn_metrics(self, final: Any = None) -> None:
        """The turn's metric block, written once: the one place that decides where it goes.

        It goes after everything the turn said (the jobs' records and the answer, whichever of them comes last) and
        directly above the closing line, so the last things on screen are the answer, the numbers, then how it
        ended. A job's record never writes it, because what follows a record is not known until the turn ends.
        The entries are the jobs' own, the turn's result and the explanation payload, each result once; ``final``
        is None for a turn that was cancelled or failed, which still shows what its jobs recorded."""
        with self._deep._lock:
            runs = list(self._deep._runs.values())
        explanation = getattr(final, "explanation", None) or {}
        entries = union_entries(
            [e for info in runs for e in info.get("metrics") or []],
            getattr(final, "metric_results", None) or [], explanation.get("metrics") or [])
        missing = (any(info.get("metric_missing") for info in runs) or getattr(final, "metric_missing", False) is True
                   or explanation.get("metric_missing") is True)
        self.write_metrics(entries, missing)

    def write_metrics(self, entries: Optional[Sequence[Mapping[str, Any]]], missing: Any = False) -> None:
        """Write the metric lines for ``entries`` that this turn's transcript does not hold yet.

        The same entry reaches the terminal more than once (a run's milestone, the turn's result, the
        explanation), and each is written once: an entry already written is skipped, however it arrived.
        ``missing`` adds the one line saying a change ended with no metric result recorded.

        The first ``METRIC_LINES_SHOWN`` metrics are written and the rest are counted, with where to open them:
        Alt+D lists all of a job's metric results. Where there is no job to open (a runner that streamed nothing),
        nothing is folded away, so no count is ever a dead end."""
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
        self.metrics_listing.extend(fresh)
        can_open = bool(self._available_deep_runs())
        lines = metric_lines(fresh, absent, METRIC_LINES_SHOWN if can_open else None, "Alt+D shows them all.")
        if lines:
            self._tlog.write(Text(""))   # a block of its own, not a continuation of the answer's last line
        for line in lines:
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
