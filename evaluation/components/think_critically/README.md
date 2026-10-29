# think_critically (metric-think-critically)

First measure for `think-critically`: is the relevant metric applied to a request by code, and is what the code finds answered?
Ten labelled requests (deep run, chat turn, in-process task; metric or none; changed or not; improved, steady or regressed). A case
passes on the measured before and after rows equal to the true readings, a response to a regression, a proposed metric where none
applies, and no invented reading. Offline: `esoi run think_critically --impl deep_runs_only`.

Arms: `deep_runs_only` (the real `GoalMetrics` over an in-memory ledger and a meter returning the case's readings; the selected
implementation), `instruction_only` (floor), `broken/claims_held` (reports every request held and improved). The result shows today's
gap on purpose: chat turns and in-process work pass through nothing, and no code proposes a metric or answers a regression
(`every_request_measured` and `measured_proposed_and_answered` are not built, so they have no arm). Kinds `task` here are in-process
work, not a queued task that starts a deep run.
