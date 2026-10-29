# Keeping the handoff page current (daily autopilot)

The handoff page is generated, never hand-edited. `render_handoff.py` reads the committed records and results and merges a
small private content file (decisions, unfinished work, notes, per-case splits) kept outside this repository. The lane's
environment names it: `QAR_HANDOFF_CONTENT` (the content file; its `page` key names the page to write).

At the END of every pass, after re-recording and `esoi tracker build`:

1. Commit the results and records first (the page states the commit it was built from).
2. Run `python evaluation/components/qar_overall/render_handoff.py`. It writes the page directly to its served path.
3. If the pass ran end_to_end or proactive_guidance, store the per-case split from the run's summary line, then render again:
   `render_handoff.py --record end_to_end "own_data 2/2, multistep 1/2"` (also `proactive_level`, `proactive_class`).
4. In the content file, append `{date, summary}` to `pass_log` (one line: what the pass measured and the floor change) and add
   anything newly unfinished to `unfinished`. Remove an item only when its metric is current and above its bar.
5. Run `render_handoff.py --check`. It exits 1 when the page is older than the latest committed results row; fix and re-run.
6. Mention the page refresh in the goal update.

Never put names, ids, real paths or the content file in this repository. No em dashes in any text added to the content file.
