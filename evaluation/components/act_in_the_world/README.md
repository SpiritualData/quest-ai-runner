# act_in_the_world (metric-act-in-the-world)

First measure for `act-in-the-world`, reusing `end_to_end`: the AI changes the world only as asked. The twelve non-terminal
`end_to_end` cases are referenced by id (`cases.json` holds ids only); a case passes when `end_to_end.score.write_problems` finds
every required change landed once on the right quest and no change nobody asked for. Offline: `esoi run act_in_the_world --impl scripted_ideal`.

Arms: `scripted_ideal` (the real orchestrator and tool catalogue with each case's ideal decisions replayed), `rules_only` (floor),
`broken/act_on_everything`. The same cases with a real model are `esoi run end_to_end --impl qar_live` (subscription calls; not run
here). Not measured here: declared reach, receipts for claimed acts, the best harness (`best_open_source_harness`), people with no
external environment.
