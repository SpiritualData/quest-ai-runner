# core_ml_tasks (metric-core-ml-tasks)

First measure for `core-ml-tasks`: how many of the solution's classification and recommendation tasks are under one measured
choice. Each task (`cases.json`) names the component and arm that is said to measure it; a task passes when that component has labelled
cases and its latest recorded row reads at or above the bar of `[balance.metric-qar-overall]`, and the result gives the recorded
accuracy. Three trap cases (a component that does not exist, an arm never recorded, a row under the bar) must come back not
measured. Offline, reads rows only: `esoi run core_ml_tasks --impl labelled_rows`.

Linked, not rebuilt: the message router is measured by `classification` (`esoi run classification --impl qar_planner`, a live arm
this component never reruns). The other tasks have no labelled case set yet, so they fail on purpose; each new labelled component
is one `component` and `arm` in `cases.json`. Arms: `labelled_rows`, `none_mapped` (floor), `broken/record_exists` (counts a task
measured because it is listed).
