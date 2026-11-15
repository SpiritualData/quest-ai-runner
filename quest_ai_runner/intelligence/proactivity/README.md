# proactivity

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > proactivity

Part of [Intelligence](../README.md)

### Top implementations of Quest Autopilot

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Scripted-client gate evaluation arm (`live_pass`) | 100% (12 of 12 cases) | free | selected |

**How it is scored:** Autopilot run gate correctness. labelled run cases (scheduled runs, Run now, automatic extra runs against the per-autopilot and team allowance, per-autopilot backpressure, the plan's cap, a paused quest, a disabled autopilot) run through the production AutopilotPass for one quest with a scripted Quest client and a pinned clock, a share held out, zero model calls; a baseline that counts every run against the budget fails them Read in this direction: maximise correct run and skip decisions against the person's settings; eliminate runs that starve a later autopilot or that stack on an unfinished run of the same autopilot; minimise spend before the decision.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Eligibility gate mixin | `quest-autopilot:autopilot_gates` | Methods on the autopilot run that decide, cheapest first, whether a run the backend scheduled goes ahead: the allowance for automatic extra runs (3 per autopilot a day and the team's budget; never a scheduled run and never Run now), backpressure per autopilot when the lane opts in, and unresolved HOLD decisions. WHEN a run is due is the backend's scheduler asking the hub's Proactivity, so nothing here reads a schedule. | [`quest_ai_runner/intelligence/proactivity/autopilot_gates.py`](autopilot_gates.py) |  |
| Work sources | `quest-autopilot:autopilot_sources` | The registered kinds of work an autopilot can be tied to by its internal source slug (today learning, which asks the hub's Learning what reviews are due). A source run states its one scope, its bounds, its standing rules and what is due in the run's brief, and a source with nothing due refuses the run, which is closed as skipped with the reason and no model call. | [`quest_ai_runner/intelligence/proactivity/autopilot_sources.py`](autopilot_sources.py) |  |
| Goal routing by scope | `quest-autopilot:autopilot_routing` | Once per quest per day, a fast-tier judgment (the lane's model provider asked by tier, never a named model) gives each eligible goal (current period, incomplete, marked for AI help) to the ONE on-duty autopilot whose scope (its purpose and prompt) it fits best, or to none. A goal a rep holds stays with an on-duty autopilot of that rep with no judgment, a failed judgment leaves the goals alone, and words never decide, they only bound what the judge reads. | [`quest_ai_runner/intelligence/proactivity/autopilot_routing.py`](autopilot_routing.py) |  |
| Structure watch binding | `quest-autopilot:structure_watch_binding` | Maps a quest's goals, experiments and investigations to the hub's structure digest and runs the hub's best-tier watch once per autopilot run of that quest, with the lane's own model provider asked by the call role's tier and its tokens and cost read per call. Every item is a decision request on the quest and no record is edited; no call is made when that quest's records did not change since its last watch, in a dry run, or when the lane switched it off (autopilot_structure_watch). Passed in by default by the poller. One real call over the hub's own records on 2026-11-10 on the best tier's default model: 1561 tokens in, 133 out, about 0.03 dollars on the keyless provider (its harness adds to the prompt's own tokens), no items; a lane with no model chosen for best fails with the reason. | [`quest_ai_runner/intelligence/proactivity/autopilot_structure_watch.py`](autopilot_structure_watch.py) |  |
| Quest AI latest look (no autopilot) | `quest-autopilot:quest_ai_latest` | A standing per-quest schedule for the living Quest AI card, for every quest whether or not autopilot is on: the hub's Proactivity says it is due when the quest's own activity, a revisit date that has passed or a fully answered card came after the last look, Prioritization holds the activity trigger back for the minimum gap after the last look and orders what to look into (an overdue look again, a check on what the person did, the plan's open question). The host passes the signals and does the look; evaluated only when the person comes back. | [`quest_ai_runner/intelligence/proactivity/quest_ai_latest.py`](quest_ai_latest.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
