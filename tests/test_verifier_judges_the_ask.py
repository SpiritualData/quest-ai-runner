"""The shared prompts judge work against the user's own ask, with no per-org or per-use-case rule.

The planner writes the done-standard and the verifier judges it. Neither names releases, deploys,
production, or any one team's workflow: a goal covers only what the request asked for, and the
verifier accepts the evidence the task naturally produces.
"""
from quest_ai_runner.core import orchestrator as o


def flat(text: str) -> str:
    return " ".join(text.split())


def test_shared_prompts_carry_no_release_or_deploy_rule():
    for prompt in (o.PLANNER_PROMPT, o.PLANNER_PROMPT_COMPACT, o.VERIFY_GOAL_PROMPT):
        t = flat(prompt).lower()
        assert "production release" not in t
        assert "not yet released" not in t
        assert "wait on a release" not in t


def test_planner_goal_covers_only_the_ask():
    for prompt in (o.PLANNER_PROMPT, o.PLANNER_PROMPT_COMPACT):
        assert "covers only what the user's own message asked for" in flat(prompt)


def test_verifier_judges_against_the_request_and_accepts_natural_evidence():
    t = flat(o.VERIFY_GOAL_PROMPT)
    assert "SCOPE AND EVIDENCE" in t
    assert "Never set met=false for something the request did not ask for" in t
    assert "a report naming what was changed" in t
    assert "only when the goal, the request, or the QUALITY STANDARDS" in t


def test_verifier_prompt_still_formats():
    rendered = o.VERIFY_GOAL_PROMPT.format(
        claims_rules="", persona="", standards="", goal="g", brief="b", transcript="",
        context="", evidence="", output="o",
    )
    assert "SCOPE AND EVIDENCE:" in rendered
