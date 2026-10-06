"""A production release is never part of "done" unless the user explicitly asked for one.

Both ends of the deep loop carry the rule: the planner that writes the done-standard, and the
verifier that judges it. Without the verifier half, a run that committed and tested its change was
still sent back as "not met" because nothing was on production yet, and the retry asked a person to
release it.
"""
from quest_ai_runner.core import orchestrator as o


def flat(text: str) -> str:
    return " ".join(text.split())


def test_planner_keeps_release_out_of_the_done_standard():
    for prompt in (o.PLANNER_PROMPT, o.PLANNER_PROMPT_COMPACT):
        t = flat(prompt)
        assert "Never make a production release, deploy, or production test part of it" in t
        assert "explicitly asked for one" in t


def test_verifier_never_fails_a_run_for_being_unreleased():
    t = flat(o.VERIFY_GOAL_PROMPT)
    assert "Never set met=false because the change is not yet released" in t
    assert "never tell the next attempt to request, ask someone for, or wait on a release" in t
    assert "explicitly asked for a production release" in t


def test_verifier_prompt_still_formats():
    rendered = o.VERIFY_GOAL_PROMPT.format(
        claims_rules="", persona="", standards="", goal="g", brief="b", transcript="",
        context="", output="o",
    )
    assert "RELEASES:" in rendered
