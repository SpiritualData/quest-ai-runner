# Keeping the handoff page current (daily autopilot)

The handoff page is generated, never hand-edited. `render_handoff.py` reads the committed records and results and merges a
small private content file (decisions, unfinished work, notes, per-case splits) kept outside this repository. The lane's
environment names it: `QAR_HANDOFF_CONTENT` (the content file; its `page` key names the page to write).

Run every `esoi run` and `render_handoff.py` with the repository's virtual environment (`.venv/bin/esoi`, `.venv/bin/python`).
It has the `hubofhubs` package installed, which the runner imports at startup; the system `python3` does not, so an arm that
imports the runner fails there before it runs a single case.

At the END of every pass, after re-recording and `esoi tracker build`:

1. Commit the results and records first (the page states the commit it was built from).
2. Run `python evaluation/components/qar_overall/render_handoff.py`. It writes the page directly to its served path.
3. If the pass ran end_to_end or proactive_guidance, store the per-case split from the run's summary line, then render again:
   `render_handoff.py --record end_to_end "own_data 2/2, multistep 1/2"` (also `proactive_level`, `proactive_class`).
4. In the content file, append `{date, summary}` to `pass_log` (one line: what the pass measured and the floor change) and add
   anything newly unfinished to `unfinished`. Remove an item only when its metric is current and above its bar.
5. Run `render_handoff.py --check`. It exits 1 when the page is older than the latest committed results row; fix and re-run.
6. Mention the page refresh in the goal update.

## Representative examples (the final pass of a staged run)

1. Run the end_to_end live arm with `QAR_E2E_TRANSCRIPTS=<scratch dir>` set (staged ladder, `--max-calls` always); never commit that directory.
2. Optional quality grade (subscription, one call per case): `.venv/bin/python evaluation/components/end_to_end/rubric.py <scratch dir> --max-calls N`.
3. Pick cases in a selection JSON kept beside the content file: `[{"case": "E2E-GOAL-01", "shows": "one line"}, ...]`.
4. `.venv/bin/python evaluation/components/qar_overall/add_examples.py --content $QAR_HANDOFF_CONTENT --transcripts <scratch dir> --select <selection.json>`.
5. Render, then `render_handoff.py --check`. Refresh the documentation excerpts first when quest-docs changed: `refresh_docs_fixture.py --docs <checkout>`.
6. The section shows only what the scratch directory holds; it never reads the repository's recorded rows for examples.

Never put names, ids, real paths or the content file in this repository. No em dashes in any text added to the content file.

When a decision resolves with a bare "approve" and no note, treat it as accepting the recommendation, apply exactly that, and record
that reading in the content file and the record you change (marked unconfirmed for the person to confirm).
