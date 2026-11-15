"""A deep goal that is not met goes on until it is met or the token budget is used, not until an attempt count: the count bounds a run only where nothing measures its spend."""
from __future__ import annotations

from hubofhubs.api import DeepResult

from .test_goal_loop_no_progress import NOT_MET, PLAN, build
from .test_per_goal_context_iteration import RecordingRunner, ScriptedProvider


WORDS = ["alpha beta gamma", "delta epsilon zeta", "eta theta iota", "kappa lambda mu", "nu xi omicron", "pi rho sigma", "tau upsilon phi", "chi psi omega", "one two three", "four five six"]
REASONS = ["the status was not reported", "a table is missing", "the owner is unnamed", "the date is wrong", "no totals shown", "the summary is empty", "links are broken",
           "the list is out of order", "a heading is missing", "units are absent"]


def results(count, tokens):
    return [DeepResult(met=True, output=WORDS[n], tokens=tokens, observations=[f"changed f{n}.txt"]) for n in range(count)]


def not_met(count):
    return [dict(NOT_MET, reason=REASONS[n]) for n in range(count)]


def test_a_measured_run_goes_past_the_attempt_count_until_met():
    provider = ScriptedProvider(plans=[PLAN], verdicts=not_met(4) + [{"met": True}])
    runner = RecordingRunner(results(5, tokens=1000))
    res = build(provider, runner, deep_goal_max_iterations=2, deep_goal_token_budget=300_000).run("what is the job status?")
    assert len(runner.calls) == 5 and res.deep_results[0].met is True


def test_the_budget_still_ends_it():
    provider = ScriptedProvider(plans=[PLAN], verdicts=not_met(10))
    runner = RecordingRunner(results(10, tokens=100_000))
    res = build(provider, runner, deep_goal_max_iterations=2, deep_goal_token_budget=300_000).run("what is the job status?")
    assert len(runner.calls) == 3 and res.deep_results[0].met is False


def test_an_unmeasured_run_keeps_the_attempt_count_as_its_budget():
    provider = ScriptedProvider(plans=[PLAN], verdicts=not_met(10))
    runner = RecordingRunner(results(10, tokens=0))
    build(provider, runner, deep_goal_max_iterations=2, deep_goal_token_budget=300_000).run("what is the job status?")
    assert len(runner.calls) == 2


def test_the_whole_output_of_an_attempt_that_fell_short_is_shown():
    long_output = " ".join(f"word{n}" for n in range(400))          # well past 600 characters
    provider = ScriptedProvider(plans=[PLAN], verdicts=not_met(1) + [{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output=long_output, tokens=1000, observations=["changed a.txt"]), DeepResult(met=True, output="done", tokens=1000, observations=["changed b.txt"])])
    events = list(build(provider, runner, deep_goal_max_iterations=2, deep_goal_token_budget=300_000).run_stream("what is the job status?"))
    shown = [getattr(e, "text", "") or (e.get("text", "") if isinstance(e, dict) else "") for e in events]
    assert any(long_output in text for text in shown)
