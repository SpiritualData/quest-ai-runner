"""The tool a worker calls to put a metric result on the run's record: ``record_metric_result``.

ESOI asks that finished work say what the relevant metric was before and after the change. This tool is where the worker
says it, as numbers handed to a function rather than prose in a closing message: the call is kept in the run's metric ledger
(``metric_results``), where the flow reads it back, checks that the run has one, and shows it to every consumer with the
rows the runner measured itself.

  * With a metric: ``metric_id`` and a number for ``before``, ``after`` or both. The row is kept as ``recorded``: nobody has
    verified the numbers, and the payload says so.
  * With no metric to report: ``not_applicable`` and a ``reason``. This is how a run on work no metric measures (a draft, an
    answer, an edit to a document) satisfies the rule honestly, so the tool works where there is no specification at all.

Every deep run is shown this tool (``ToolSpec.standing_deep``) and no chat turn is (``on_request_only``). A call is kept under
the run's id, which the brief tells the worker to pass as ``--run``; with only ``--task`` it is kept under the task. The tool
checks a value is a number and nothing else: a string, a boolean or a non-finite number is refused, never converted.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from esoi import SOURCE_NOT_APPLICABLE, SOURCE_RECORDED, FileMetricLedger, MetricResult, payload_of, render_line
from esoi import describe_metric
from hubofhubs.api import default_directory

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.repo_specs import root_for
from quest_ai_runner.the_world.tools.tools import (STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec,
                                                   quest_client_from_env, quest_credentials_present)

TOOL_NAME = "record_metric_result"
LEDGER_DIR_ENV = "QAR_METRIC_LEDGER_DIR"
KEYWORDS = ("metric", "result", "before", "after", "milestone", "measure", "record")

# WHICH metric a run records. Said once here and used by the tool's own guidance, the deep brief's closing line and the one
# nudge (goal_metrics.py), because a run that picks the wrong metric meets the rule on paper and says nothing: a run that
# changed a quest's structure recorded the quest's paid-subscriber count, 0 before and 0 after, which no structural change
# could move that day. The relevant metric is the one of the solution the change implements; an outcome further down the
# chain is context for the reason, not the result.
WHICH_METRIC = ("Which metric: the one of the solution your change implements (the solution whose implementation, records, "
                "prompts or code you changed), not an outcome further down the chain that this change cannot move within "
                "the run; such an outcome belongs in the reason as context. If that solution has no metric, defining one "
                "is part of the work: add it to the specification (apply_spec_edit, a metrics record linked to the "
                "solution) and record its value on the state before and after your change. not_applicable only when no "
                "solution covers the change.")


def metric_ledger_from_env(env: Mapping[str, str]) -> FileMetricLedger:
    """The ledger ``QAR_METRIC_LEDGER_DIR`` names (default: a folder in the system temp directory, like the tool ledger)."""
    return FileMetricLedger(env.get(LEDGER_DIR_ENV) or default_directory("qar-metric-ledger"))


def truthy(value: Any) -> bool:
    return value is True or value == "true"


def record_metric_result_spec(env: Mapping[str, str]) -> ToolSpec:
    ledger = metric_ledger_from_env(env)

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        run_id = str(ctx.value("run_id") or ctx.task_id or "").strip()
        if not run_id:
            return ToolResult(ok=False, text="Needs this run's id: add --run <id> to the call (the id is in this run's "
                                             "tool list).")
        try:
            result = build_result(args, run_id, ctx.task_id or "", lambda metric_id: quest_metric_names(env, ctx.quest_id, metric_id))
        except ValueError as e:
            return ToolResult(ok=False, text=f"Not recorded: {e}.")
        if not ledger.record(result):
            return ToolResult(ok=False, text="Not recorded: the run's metric record could not be written.")
        return ToolResult(ok=True, data=payload_of([result]),
                          text="Recorded. " + render_line(payload_of([result])))

    return ToolSpec(
        name=TOOL_NAME, handler=handler,
        description=("Put the metric result of your change on this run's record: the metric's value before and after, or "
                     "that no metric applies. The runner shows it beside the values it measured itself."),
        when_to_use=("Before you finish work that changes something: pass the metric's before and after numbers (one end "
                     "is fine, and a value the metric's cascading conditionals did not call for stays out, never estimated; the ids "
                     "come from the brief or the specification records), or not_applicable with a one-sentence reason if no "
                     "metric applies. " + WHICH_METRIC),
        when_not_to_use="Anything that is not a metric value: findings and summaries go in your final message.",
        parameters={"type": "object", "properties": {
            "metric_id": {"type": "string", "description": "The metric's id (needed unless not_applicable)."},
            "solution_id": {"type": "string", "description": "The solution it serves, if known."},
            "milestone": {"type": "string", "description": "The milestone this moves, in a few words."},
            "before": {"type": "number", "description": "Value before your change (a number only)."},
            "after": {"type": "number", "description": "Value after your change (a number only)."},
            "not_applicable": {"type": "boolean", "description": "true if no metric applies (give a reason, no values)."},
            "reason": {"type": "string", "description": "Why none applies, how you measured, or which cascade stage was not run and why."}}},
        mutates=False, origin="standard", workflow_step=False, keywords=KEYWORDS, timeout_seconds=30.0,
        on_request_only=True, standing_deep=True)


def quest_metric_names(env: Mapping[str, str], quest_id: Optional[str], metric_id: str) -> Dict[str, str]:
    """``{"name", "solution_id"}`` of a metric kept in a quest's own specification records, ``{}`` when it is not there.

    The local lookup (``describe_metric``) sees only a repository's ``esoi/`` files; a quest's records live in Quest, so a
    result recorded against a quest metric would otherwise carry no name and a screen would have only its id to show.
    Never raises: a name is a nicety, the result is kept either way."""
    if not quest_id or not quest_credentials_present(env):
        return {}
    try:
        from quest_ai_runner.the_body.internal_context.quest.spec_records import QuestSpecRecords
        row = (QuestSpecRecords(quest_client_from_env(env)).read(quest_id).get("metrics") or {}).get(metric_id) or {}
    except Exception:  # noqa: BLE001
        return {}
    solutions = row.get("solution_ids") or []
    return {key: value for key, value in (("name", str(row.get("name") or "").strip()),
                                          ("solution_id", str(solutions[0]) if solutions else "")) if value}


def build_result(args: Mapping[str, Any], run_id: str, task_id: str,
                 quest_lookup: Optional[Callable[[str], Dict[str, str]]] = None) -> MetricResult:
    """The row a call stands for; ValueError (its message names what to fix) when the call is not a valid result.

    The metric's name (and solution, when the call gave none) comes from the repository's records, else from
    ``quest_lookup`` (the quest's own records), so a result is never kept under an id alone when a name exists."""
    reason = str(args.get("reason") or "").strip()
    if truthy(args.get("not_applicable")):
        return MetricResult(source=SOURCE_NOT_APPLICABLE, run_id=run_id, task_id=task_id,
                            solution_id=str(args.get("solution_id") or ""), reason=reason,
                            before=args.get("before"), after=args.get("after"))
    metric_id = str(args.get("metric_id") or "").strip()
    if not metric_id:
        raise ValueError("metric_id is needed (or not_applicable with a reason)")
    known: Dict[str, str] = describe_metric(metric_id, None, find_root=root_for)
    if not known.get("name") and quest_lookup is not None:
        known = {**quest_lookup(metric_id), **{k: v for k, v in known.items() if v}}
    return MetricResult(source=SOURCE_RECORDED, run_id=run_id, task_id=task_id, metric_id=metric_id,
                        solution_id=str(args.get("solution_id") or known.get("solution_id") or ""),
                        name=known.get("name", ""), milestone=str(args.get("milestone") or ""),
                        before=args.get("before"), after=args.get("after"), reason=reason)


def standard_builder(env: Mapping[str, str]) -> Optional[ToolSpec]:
    return record_metric_result_spec(env)


STANDARD_TOOL_BUILDERS[TOOL_NAME] = standard_builder
