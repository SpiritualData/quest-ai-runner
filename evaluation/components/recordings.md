# Recording provenance for `results.csv`

`results.csv` and `per_case.csv` are written by `esoi run <component> --impl <arm> --record` (or `esoi run --stale`). A row is
trustworthy only if it can be re-made, so every batch of re-recorded arms gets one entry here, in the same commit as the rows.
Per-row facts (`git_sha` = the measured code the arm ran against, `cases_sha`, `model_calls`, `seconds`) are already columns in
`results.csv`; this file adds what a bare row cannot say: which commit the batch was recorded at, the exact arm list, and how to
check it. Never edit a row by hand; re-run the arm.

How to check an entry: `git checkout <recorded at>` (or a worktree), run each command listed, and compare the new row's `passed`,
`cases` and `cases_sha` with the committed one (`seconds` varies). A mismatch means the committed row is stale or was edited.

## 2026-11-23: seven free arms, 0 model calls

- Recorded at measured-code commit: `b4c2e7fd` ("Rename _keep_valid to keep_valid in autopilot_kpi_report"), the `git_sha` of all seven rows.
- Committed in: `bcf5c6f9` (50 changed lines in `per_case.csv`, 7 new rows in `results.csv`).
- Model calls: 0 for every arm (the `model_calls`, `tokens_in`, `tokens_out` and `cost_usd` columns are 0; none is a live arm).

| Component | Arm | Cases | Passed | `cases_sha` |
|---|---|---|---|---|
| run_queued_tasks | qar_runner | 43 | 43 | 00f3bf83 |
| escalate_to_a_human | quest_decision_sink | 15 | 15 | 597d556f |
| write_files | bounded_writer | 20 | 20 | 5a1bc756 |
| keep_secrets_out | live_scanner | 35 | 35 | 17d7b83a |
| expose_the_public_api | live_surface | 73 | 73 | 50efdb5a |
| learn_by_example | live_examples | 37 | 37 | 80050d75 |
| hub_of_hubs | registry_scan | 278 | 257 | bc61fb2a |

Command for each: `esoi run <component> --impl <arm> --record`. `hub_of_hubs` 257/278 is a real shortfall in the registry scan
(by_config, fake_hub, seam and selectable cases), recorded as measured, not a staleness artefact.
