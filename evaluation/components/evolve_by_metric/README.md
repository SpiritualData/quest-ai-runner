# evolve_by_metric (QME7's command)

First measure for `evolve-by-metric`: does the mechanism that lets a better part replace the current one by its metric decide
correctly, and does the running system follow the records? Seventeen cases under the library's rule (a switch needs a statistically significant gain): eleven synthetic leaderboards (a
significant paired win switches, the 40-case pair being significant paired and not by Fisher; a non-significant paired gap, a tie, a rival
on other cases, a worse rival and a cost difference at equal quality keep the incumbent; aggregate-only rows use Fisher's exact test;
a stricter solution-level requirement blocks a switch the default would allow; an unmeasured selection is said so), two first selections
(a labelled best guess; an exact tie selects nothing), three synthetic snapshot drifts, and the repository's own committed selection
snapshot against the records and `core.resolve`.
Offline, zero model calls: `esoi run evolve_by_metric --impl structure_library`.

Arms: `structure_library` (the esoi library's own leaderboard and runtime, live), `keep_current` (floor: never switches, never
checks the snapshot), `broken/newest_row_wins` (highest raw pass rate wins, any cases, no test). QME6 and QME7 of `qar_overall` still read the
structure themselves; this component checks the mechanism they rely on. Not covered: whether a person adopts a promotion.
