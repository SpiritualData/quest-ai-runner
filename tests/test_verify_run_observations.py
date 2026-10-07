"""The verifier must see a deep run's OWN structured evidence (``DeepResult.observations``), not
only the worker's output text and brief (live finding, 2026-10-06): a Quest chat turn's code
runner really added a goal (the database diff showed it, and the run's own ``DeepResult
.observations`` carried the receipt line "Added the goal ..."), yet ``Orchestrator._verify_goal``
judged the worker's OUTPUT TEXT and brief alone, saw no evidence, and returned met=False with "the
worker claimed to add a goal ... but ... no evidence" -- so "Goal not met: ..." reached the chat
right next to the correct reply.

Same shape as ``tests/test_verify_web_evidence.py``'s web-evidence fix: a deep run's own receipts
(write confirmations, reads that returned content -- built by the RUNNER from its own bookkeeping,
never from the wording of its output, see ``core/adapters.DeepResult``) are rendered in the
verify prompt's volatile tail as a labeled EVIDENCE section, gated strictly on
``DeepResult.observations_reported`` so a runner that does not report observations leaves the
prompt byte-for-byte unchanged.

Covers:
(a) direct unit tests on ``_verify_goal``'s new ``observations``/``observations_reported``
    parameters: absent, or ``observations_reported=False`` (however ``observations`` is set), is
    byte-for-byte the old prompt; ``observations_reported=True`` renders the receipts (capped,
    reusing ``VERIFY_WEB_EVIDENCE_MAX_CHARS``) with the run-record precedence note, before the
    WORKER OUTPUT section; an empty list with ``observations_reported=True`` renders an explicit
    "none recorded" line rather than nothing, so the verifier can read a write goal's lack of a
    receipt as proof the write did not happen.
(b) end to end through the real deep-goal loop (``Orchestrator._run_deep`` -> ``_verify_goal``):
    a scripted runner's ``DeepResult.observations`` reaches the verify prompt the SAME call sends
    to the verifier, and a run that does not report observations leaves that prompt unchanged.

All offline: no network, no API key.
"""
import re
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.orchestrator import (
    OrchestratorConfig,
    VERIFY_RUN_OBSERVATIONS_NOTE,
    VERIFY_WEB_EVIDENCE_MAX_CHARS,
)

from .conftest import StubProvider

from .test_verify_context_layer import LayeredScriptedProvider, RecordingRunner, make_orch


# --------------------------------------------------------------------------- #
# (a) _verify_goal: direct unit tests
# --------------------------------------------------------------------------- #

def test_verify_goal_with_no_observations_args_is_byte_for_byte_unchanged():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the output")
    assert verdict is not None and error is None
    assert "THIS RUN'S OWN RECORD" not in provider.last_plan_prompt
    assert "RUN-RECORD PRECEDENCE" not in provider.last_plan_prompt


def test_verify_goal_observations_reported_false_is_unchanged_even_with_a_populated_list():
    # observations_reported=False means the runner cannot tell -- a populated list must behave
    # identically (nothing rendered) to no observations at all when the flag is off.
    baseline_provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    make_orch(baseline_provider)._verify_goal("the goal", "the brief", "the output")

    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal(
        "the goal", "the brief", "the output",
        observations=["Added the goal 'Ship v2' to quest Q1."], observations_reported=False)

    assert verdict is not None and error is None
    assert provider.last_plan_prompt == baseline_provider.last_plan_prompt
    assert "THIS RUN'S OWN RECORD" not in provider.last_plan_prompt


def verify_prompt(observations: Optional[List[str]], observations_reported: bool) -> str:
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal(
        "the goal", "the brief", "the output",
        observations=observations, observations_reported=observations_reported)
    assert verdict is not None and error is None
    return provider.last_plan_prompt


def test_verify_goal_observations_reported_true_renders_the_receipts():
    prompt = verify_prompt(["Added the goal 'Ship v2' to quest Q1.",
                            "Read back quest Q1 and confirmed the new goal is present."], True)
    assert "THIS RUN'S OWN RECORD" in prompt
    assert "Added the goal 'Ship v2' to quest Q1." in prompt
    assert "Read back quest Q1 and confirmed the new goal is present." in prompt
    assert "RUN-RECORD PRECEDENCE" in prompt
    assert VERIFY_RUN_OBSERVATIONS_NOTE.strip() in prompt
    # Evidence must sit before the output it grounds, same convention as the web evidence block.
    evidence_idx = prompt.index("THIS RUN'S OWN RECORD")
    output_idx = prompt.index("--- WORKER OUTPUT")
    assert evidence_idx < output_idx


def test_verify_goal_observations_reported_true_with_empty_list_says_none_recorded():
    # The structural fact a write goal needs: an EMPTY receipt list on a run that DOES report
    # observations is proof the write did not happen, not absence of evidence.
    prompt = verify_prompt([], True)
    assert "THIS RUN'S OWN RECORD" in prompt
    assert "none recorded" in prompt
    assert "RUN-RECORD PRECEDENCE" in prompt


def test_verify_goal_observations_reported_true_with_none_list_also_says_none_recorded():
    prompt = verify_prompt(None, True)
    assert "none recorded" in prompt


def test_verify_goal_run_observations_is_capped():
    long_lines = [f"wrote record {i}: " + ("x" * 2000) for i in range(10)]
    prompt = verify_prompt(long_lines, True)
    start = prompt.index("THIS RUN'S OWN RECORD")
    end = prompt.index("RUN-RECORD PRECEDENCE")
    assert end - start < VERIFY_WEB_EVIDENCE_MAX_CHARS + 600
    assert "context truncated for verification" in prompt


def test_verify_goal_run_observations_coexists_with_web_evidence():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    gathered = [{"kind": "query", "rel_path": "web_search:q", "text": "WEB RESULTS: LIVE_FACT."}]
    verdict, error = orch._verify_goal(
        "the goal", "the brief", "the output", gathered=gathered,
        observations=["Added the goal."], observations_reported=True)
    assert verdict is not None and error is None
    prompt = provider.last_plan_prompt
    assert "EVIDENCE GATHERED THIS TURN" in prompt
    assert "LIVE_FACT" in prompt
    assert "THIS RUN'S OWN RECORD" in prompt
    assert "Added the goal." in prompt


# --------------------------------------------------------------------------- #
# (b) End to end through the real deep-goal loop
# --------------------------------------------------------------------------- #

RECEIPT = "Added the goal 'Ship v2' to quest Q1."

# Each deep subtask's brief carries a freshly-generated ``TASK [xxxxxxxx]`` id (see
# ``orchestrator.py``'s ``task_uuid``), unrelated to this fix, so two separate ``run()`` calls
# never produce byte-identical verify prompts. Normalize it out before comparing.
TASK_ID_RE = re.compile(r"TASK \[[0-9a-f]{8}\]")


def normalize_task_id(text: str) -> str:
    return TASK_ID_RE.sub("TASK [xxxxxxxx]", text)


def run_one_deep_goal(result: DeepResult, verdict: Dict[str, Any]):
    plan = {"action": "deep", "goal": "Add a goal to the quest",
            "deep_subtasks": [{"goal": "Add a goal to the quest", "brief": "add it"}],
            "rationale": "deep"}
    provider = LayeredScriptedProvider(plans=[plan], verdicts=[verdict])
    runner = RecordingRunner([result])
    orch = make_orch(provider, deep_runner=runner,
                     config=OrchestratorConfig(deep_goal_max_iterations=3))
    res = orch.run("add a goal to the quest")
    return res, provider


def test_deep_goal_loop_receipt_reaches_the_verify_prompt():
    result = DeepResult(met=False, output="I added the goal as requested.",
                        observations=[RECEIPT], observations_reported=True)
    res, provider = run_one_deep_goal(result, {"met": True, "reason": "the receipt proves it"})

    assert res.kind == "deep"
    assert len(provider.verify_prompts) == 1
    assert RECEIPT in provider.verify_prompts[0]
    assert "THIS RUN'S OWN RECORD" in provider.verify_prompts[0]


def test_deep_goal_loop_without_observations_reported_leaves_verify_prompt_unchanged():
    reported = DeepResult(met=False, output="I added the goal as requested.",
                          observations=[RECEIPT], observations_reported=False)
    not_reported_res, not_reported_provider = run_one_deep_goal(
        reported, {"met": True, "reason": "done"})

    bare = DeepResult(met=False, output="I added the goal as requested.")
    bare_res, bare_provider = run_one_deep_goal(bare, {"met": True, "reason": "done"})

    assert (normalize_task_id(not_reported_provider.verify_prompts[0])
            == normalize_task_id(bare_provider.verify_prompts[0]))
    assert "THIS RUN'S OWN RECORD" not in not_reported_provider.verify_prompts[0]
