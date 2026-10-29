# hub_of_hubs

The metric of `use-open-source-hubs`: is QAR a hub of hubs? It reads `esoi/hubs.csv` (one row per area), `esoi/hub_findings.csv`
(each researched candidate, with license, link, date, source quality) and `esoi/hub_capabilities.csv` (what QAR can do through
a hub, and whether that needed code), plus the code. Free and offline.

```
esoi run hub_of_hubs --impl registry_scan --record          # the live reading (scored per area and check)
esoi run hub_of_hubs --impl hub_of_hubs.broken.vendor_grep:build    # the floor: grep for vendor names
python3 evaluation/components/hub_of_hubs/render_docs.py    # rewrite the generated table in docs/hub-of-hubs.md
.venv/bin/python3 -m pytest evaluation/components/test_hub_of_hubs.py
```

Files: `hub_capabilities.csv` lists hub CONNECTIONS (a class gained through one hub, counted once; `wired` no does not count),
`hub_integrations.csv` lists single-service integrations QAR hand-wires and which hub covers them (a waiver needs a reason).
Numbers: coverage; integration-by-config share; no reinvented integration; research age per area (max and mean, bar 90 days); code structure (one seam per
area, selectable by metric, vendor branches in `quest_ai_runner/core`, a fake hub accepted by configuration). The stated scan
rule is in `scan.py`. Add an area by adding a row, its findings and the eight cases (the test names what is missing).

## Autopilot backlog item (instruction text for the daily pass)

Hub research, once per day, at most one area, no paid model calls (the Claude Code subscription and WebSearch only):

1. Run `esoi run hub_of_hubs --impl registry_scan` and read the "stale areas" list; pick the area with the oldest
   `last_researched` in `esoi/hubs.csv` (or a stale one first). If none is within 14 days of the 90-day bar, stop.
2. Search the web in standard mode for open-source hubs for that area that were released or changed since its
   `last_researched`: prefer official repositories and documentation, and cite the URL and the date of each. Look for what QAR
   could incorporate by configuration and for alternatives to compare as an implementation arm.
3. Replace that area's rows in `esoi/hub_findings.csv` (2 to 4 candidates, license, role, one line, evidence URL, today's date;
   say when a license is unconfirmed) and set `last_researched`, `researched_by` and `recommendation` in `esoi/hubs.csv`.
   Update `esoi/hub_capabilities.csv` (a hub connection, never a single service) and `esoi/hub_integrations.csv` (retire a row when
   its adapter is removed in favour of a hub). Never invent adoption: a hub is "builds_on"
   only if the code uses it today.
4. Run `python3 evaluation/components/hub_of_hubs/render_docs.py` (rewrites the table in `docs/hub-of-hubs.md` in place), the
   component test, and `esoi hygiene`. No names, ids, emails or machine paths in anything committed; no em dashes.
5. Commit only those files, on the current month branch, with an explicit-path `git add`. If a recommendation is worth
   building, add or update a Quest decision-request instead of building it unasked.
