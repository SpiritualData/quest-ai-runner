# evolve_by_metric (QME7's command)

First measure for `evolve-by-metric`: does the mechanism that lets a better part replace the current one by its metric decide
correctly, and does the running system follow the records? Eleven cases: seven synthetic leaderboards (a clear win switches, a tie
or a rival on other cases does not, a cheaper equal switches, a cheaper worse one does not, an unmeasured selection is said so),
three synthetic snapshot drifts, and the repository's own committed selection snapshot against the records and `core.resolve`.
Offline, zero model calls: `esoi run evolve_by_metric --impl structure_library`.

Arms: `structure_library` (the esoi library's own leaderboard and runtime, live), `keep_current` (floor: never switches, never
checks the snapshot), `broken/newest_row_wins` (highest raw pass rate wins, any cases). QME6 and QME7 of `qar_overall` still read the
structure themselves; this component checks the mechanism they rely on. Not covered: whether a person adopts a promotion.
