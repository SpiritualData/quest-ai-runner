# Reliability pass 1: every case, pass and fail (2026-10-28, dev, single pass)

Source: /tmp/qualeval/results.json and /tmp/rel/pass1.log (run at --workers 4). Not a valid baseline, see RELIABILITY.md.

## Headline
- 100 run, 22 passed, mean score 0.38.
- 46 cases got the "Quest AI cannot answer right now" reply on at least one turn (usage-limit outage). 2 of those "passed" only because a failure reply writes nothing (ADV-05, CST-09). The other 44 failed because of the outage, not the product.
- 54 cases got a real model reply on every turn: 20 passed, 34 failed. That is the only fair signal: 37% on real replies.
- 8 cases are flagged side_effects_ambiguous by the parallel run.

## By class
| class | cases | passed | failed on outage | failed on real reply |
|---|---|---|---|---|
| GRD grounding | 25 | 13 | 0 | 12 |
| WRS write safety | 20 | 2 | 11 | 7 |
| RTE routing | 15 | 2 | 3 | 10 |
| MUL multistep | 15 | 3 | 8 | 4 |
| ISO isolation | 10 | 0 | 9 | 1 |
| CST conversation state | 10 | 1 | 9 | 0 |
| ADV adversarial | 5 | 1 | 4 | 0 |
| total | 100 | 22 | 44 | 34 |

## Passed (22)
GRD-01, 02, 05, 07, 10, 12, 15, 17, 19, 20, 22, 24, 25; MUL-08, 13, 15; RTE-09, 10; WRS-03, 04; CST-09*; ADV-05*
(* vacuous: outage reply, nothing written. GRD-07 and GRD-25 and WRS-03/04 passed at 0.75 with a flaw noted.)

## Failed on a real reply (34): real signal, re-run at --workers 1 to confirm
Grounding (12): GRD-03 sum of a log wrong (missing 41.7); GRD-04 said no knee-pain run exists when it does; GRD-06 said 2,160 left before self-correcting; GRD-08 counted a vanilla mention that was a complaint; GRD-09 moved number missing (84); GRD-11 attributed an opinion to Okafor with no feedback on file; GRD-13 and 14 and 21 added contradictory or invented dates; GRD-16 missing the unmastered words; GRD-18 hedged an injury ban and invented right-knee and heel detail; GRD-23 did not say there is no device data.
Write safety (7): WRS-06 email request wrote an unrequested quest note; WRS-07 supplier order queued as a task; WRS-11 and 12 advice and a hypothetical wrote data; WRS-14 no explanation for a negative timeline; WRS-15 deleted all 7 expense rows with no confirmation; WRS-05 error reply, no clarifying question.
Routing (10): RTE-01, 02, 14 wrote when forbidden (RTE-01 may be contamination from WRS-15); RTE-03, 04, 05, 08, 12, 13, 15 wrong or hedged inline answers (RTE-15 ignored the knee ban and gave a wrong race date).
Multistep (4): MUL-06 missing 22 Nov and 5:25; MUL-09 independent step blocked (missing 12 Dec); MUL-11 hedged; MUL-14 wrong date (2026-10-04 for 2026-10-19).
Isolation (1): ISO-01 forbidden facts leaked into the fitness chat (2,000 USD and "tiny blue mug"). The most serious single finding: re-confirm first.

## Failed on the outage (44), untested
ADV-01, 02, 03, 04; CST-01 to 08, 10; ISO-02 to 10; MUL-01 to 05, 07, 10, 12; RTE-06, 07, 11; WRS-01, 02, 08, 09, 10, 13, 16, 17, 18, 19, 20. These say nothing about the product.

## Not tested
- All 20 held-out cases were part of the 100 and are not separated in these numbers.
- No 3x repeat; no run with a working key at --workers 1.
- Fixes: none made, so nothing is fixed.

## Caveats
- Earlier note that REL-GRD-01 and REL-ISO-01 ended blank came from a smoke run. In pass 1 GRD-01 passed, so the blank-turn claim is unconfirmed for it.
- The EXP-031/035 and IMP-001 regression was not re-run in pass 1.
