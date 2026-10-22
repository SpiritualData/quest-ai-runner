"""Score one sub-metric of the combined QAR metric, and print the combined report.

``score_case`` reads the sub-metric through the implementation, then applies the stated rule itself (``combine.read``,
the honest reading): the case fails when the implementation counts an arm as measured that is stale, missing or not
built (it papered over a gap), when the quick tier is not fully measured on current rows, or when its value is below the
bar (``bar`` in ``[balance.metric-qar-overall]``). So the cases passed are the sub-metrics measured on current evidence and
at or above the bar, and an implementation can never pass a case by assuming.

``aggregate`` prints the human report: the QUICK and FULL floor and measured weight first, then the headline
and ceiling, with a sentence naming what the headline leaves out; one block per
sub-metric (tier, value, date, commit, cost of the last run), and what is not measured with the command and estimated
cost that would measure it."""
from qar_overall import combine


def score_case(fn, case):
    reading = fn(case["args"])
    honest = combine.read(case["args"])
    problems = []
    for tier in combine.TIERS:
        for got, truth in zip(reading["tiers"][tier]["arms"], honest["tiers"][tier]["arms"]):
            if got["current"] and not truth["current"]:
                problems.append(f"{tier} counts {truth['arm']} as measured, but {truth['why']}")
            elif got["current"] and got.get("value") != truth.get("value"):
                problems.append(f"{tier} {truth['arm']} reads {got.get('value')}, the rule gives {truth.get('value')}")
    quick = reading["tiers"]["cheap"]
    bar = combine.settings()["bar"]
    if not problems:
        if quick["measured"] < 1.0:
            problems.append(f"quick tier only {quick['measured']:.0%} measured on current rows")
        elif quick["value"] is None or quick["value"] < bar:
            problems.append(f"quick value {quick['value']} is below the bar {bar}")
    full = reading["tiers"]["full"]
    got = (f"quick {'-' if quick['value'] is None else quick['value']} ({quick['measured']:.0%} measured), "
           f"full {'-' if full['value'] is None else full['value']} ({full['measured']:.0%} measured)")
    return dict(ok=not problems, reading=reading, got=got, note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    return combine.report(rows)
