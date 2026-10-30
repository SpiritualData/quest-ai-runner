"""A request that reads and then changes keeps what the read found when the change is parked on approval.

Cause found in a recorded chat miss: "check my log for anything I should know, then add a goal that fits"
read the log, answered with what it held, and deferred the write. The write parked on an approval card,
and the parked-only branch replaced the whole reply with the card's own wording, so the person saw an
approval prompt that never said what the read had found (a stored injury). Now the answer that was written
from this turn's reads stays in front of the ask. A turn that read nothing is unchanged: the ask is still
the whole reply, verbatim.

Fully offline, no model calls.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

ASK = ("Approve this change to your Quest data?\n\nI will add a goal named \"Swim 2 km\" to your quest. "
       "Do you approve this change?")
FINDING = "Your log shows a knee injury on 26 Oct and a ten day ban on running."


def run(decisions, files=None):
    provider = StubProvider(decisions=decisions, answer_text=FINDING)
    runner = StubDeepRunner(met=False, decision_id="dec_swim", output=ASK)
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval(files or {}), provider=provider,
                        registry=ModelRegistry(provider), config=config, deep_runner=runner)
    return orch.run("Check my log for anything I should know, then add a goal that fits")


def test_the_findings_stay_in_front_of_the_parked_ask_when_the_turn_read_something():
    res = run([
        {"action": "read", "reads": [{"rel_path": "log.md"}], "rationale": "read the log", "user_intent": "act"},
        {"action": "answer", "rationale": "report then change", "user_intent": "act",
         "deferred_deep": {"goal": "Add a goal that fits the log"}}],
        files={"log.md": "GROUNDING knee injury, no running for 10 days"})
    assert res.exit_reason == "parked" and res.decision_id == "dec_swim"
    assert res.text.startswith(FINDING)
    assert res.text.endswith(ASK)
    assert res.text.count(ASK) == 1


def test_a_turn_that_read_nothing_still_replies_with_the_ask_verbatim():
    res = run([{"action": "answer", "rationale": "answer then change", "user_intent": "act",
                "deferred_deep": {"goal": "Add a goal"}}])
    assert res.exit_reason == "parked"
    assert res.text == ASK
