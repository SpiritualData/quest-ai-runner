"""The user-facing "Explain how I got this" panel (see core/answer_explanation.py).

What these tests pin down, in the order the design depends on it:

1. ELIGIBILITY IS MODEL-FREE AND EXCLUDES SMALL TALK. A turn that read nothing, executed nothing
   and answered at the first step gets no panel, and costs no call to find that out.
2. THE PANEL IS EMITTED AFTER THE ANSWER, NEVER BEFORE. This is the whole reason a second call is
   cheaper than folding the explanation into the answer: by the time it runs, the answer has
   already left. A test that only checked "the event exists" would not catch a regression that
   moves it in front of the result.
3. THE VERIFIABLE HALF SURVIVES A FAILED GENERATION. ``used`` and ``signals`` come from the real
   trace, so a provider blowing up costs the prose sections and nothing else.
4. IT IS OFF BY DEFAULT. With the flag off the run emits exactly what it emitted before.

Offline, no network.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import EVENT_DONE, EVENT_EXPLANATION, EVENT_RESULT
from quest_ai_runner.core.answer_explanation import (
    TurnTrace,
    build_payload,
    is_eligible,
    render_record_for_prompt,
    render_used,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Mode, Orchestrator, OrchestratorConfig
from quest_ai_runner.core import StreamSink

from .conftest import StubProvider, StubRetrieval


class RecordingSink(StreamSink):
    """A real StreamSink that keeps every event in order, so ORDERING can be asserted.

    Subclasses the library's own sink rather than duck-typing one, so the run's emission POLICY
    (which events a LIVE run forwards at all) is exercised for real.
    """

    def __init__(self) -> None:
        self.events: List[Any] = []
        super().__init__(self.events.append)

    def types(self) -> List[str]:
        return [e.get("type") for e in self.events]

    def of_type(self, event_type: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("type") == event_type]


class ExplainingProvider(StubProvider):
    """A provider whose plan() returns the explanation object when handed the explain schema."""

    def __init__(self, decisions: List[Dict[str, Any]], explanation: Dict[str, Any]):
        super().__init__(decisions=decisions)
        self.explanation = explanation
        self.explain_prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if (tool_schema or {}).get("name") == "answer_explanation":
            self.explain_prompts.append(prompt)
            return dict(self.explanation)
        return super().plan(prompt, model=model, tool_schema=tool_schema)


class ExplainRaisingProvider(StubProvider):
    """Blows up on the explanation call only, leaving the rest of the turn intact."""

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if (tool_schema or {}).get("name") == "answer_explanation":
            raise RuntimeError("explain provider blew up")
        return super().plan(prompt, model=model, tool_schema=tool_schema)


SAMPLE_EXPLANATION = {
    "understood": "You wanted to know what the readme says about metrics.",
    "approach": "Read the one document that covers it and summarized the relevant part.",
    "assumptions": ["That the readme is current."],
    "confidence": "High for what the document states, since it was read directly.",
    "limitations": ["Only one document was checked."],
    "what_would_change": ["A newer version of the document saying something different."],
}


def orch(provider, **kw) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval(kw.pop("files", None) or {}), provider=provider,
                        registry=ModelRegistry(provider), **kw)


# --- 1. eligibility: model-free, and small talk is excluded for free ---------------------------

def test_small_talk_turn_is_not_eligible():
    """Nothing read, nothing executed, answered at step 1: there is nothing to account for."""
    trace = TurnTrace(kind="answer", user_message="Hi", answer="Hello.", steps=1)
    assert is_eligible(trace) is False


def test_a_turn_that_read_something_is_eligible():
    trace = TurnTrace(kind="answer", answer="Yes.", steps=1,
                      gathered=[{"kind": "read", "rel_path": "README.md", "locator": "head"}])
    assert is_eligible(trace) is True


def test_a_turn_that_executed_an_action_is_eligible():
    trace = TurnTrace(kind="answer", answer="Done.", steps=1,
                      actions=[{"goal": "save the note", "succeeded": True, "failed": False}])
    assert is_eligible(trace) is True


def test_a_turn_that_answered_on_assembled_context_is_eligible():
    trace = TurnTrace(kind="answer", answer="Yes.", steps=1,
                      cards=[{"title": "Metrics card", "adapter": "files"}])
    assert is_eligible(trace) is True


def test_a_multi_step_turn_is_eligible():
    trace = TurnTrace(kind="answer", answer="Yes.", steps=3)
    assert is_eligible(trace) is True


def test_a_web_search_turn_is_eligible():
    trace = TurnTrace(kind="answer", answer="Yes.", steps=1,
                      gathered=[{"kind": "query", "rel_path": "web_search:current rates"}])
    assert is_eligible(trace) is True


def test_a_confirm_turn_is_never_eligible():
    """A confirm is a question, not an answer. There is no answer to explain."""
    trace = TurnTrace(kind="confirm", answer="", steps=2,
                      gathered=[{"kind": "read", "rel_path": "README.md"}])
    assert is_eligible(trace) is False


def test_an_error_observation_alone_does_not_make_a_turn_eligible():
    """A read that FAILED is not information the answer used."""
    trace = TurnTrace(kind="answer", answer="Sorry.", steps=1,
                      gathered=[{"kind": "error", "rel_path": "missing.md", "error": "not found"}])
    assert is_eligible(trace) is False


# --- 2. the panel is emitted AFTER the answer, and only when eligible --------------------------

def test_explanation_is_emitted_after_the_result_and_before_done():
    """ORDERING IS THE FEATURE. The answer must already be out when the explanation is written."""
    provider = ExplainingProvider(
        decisions=[{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look"},
                   {"action": "answer", "rationale": "have it"}],
        explanation=SAMPLE_EXPLANATION)
    sink = RecordingSink()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1))
    o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=sink)

    types = sink.types()
    assert EVENT_EXPLANATION in types, f"no explanation emitted; got {types}"
    assert EVENT_RESULT in types
    assert types.index(EVENT_RESULT) < types.index(EVENT_EXPLANATION), (
        "the explanation must come AFTER the answer, or it delays what the reader is waiting for")
    assert types.index(EVENT_EXPLANATION) < types.index(EVENT_DONE)


def test_explanation_payload_carries_the_written_sections_and_the_real_record():
    provider = ExplainingProvider(
        decisions=[{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look"},
                   {"action": "answer", "rationale": "have it"}],
        explanation=SAMPLE_EXPLANATION)
    sink = RecordingSink()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1))
    res = o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=sink)

    payload = sink.of_type(EVENT_EXPLANATION)[0]["data"]
    assert payload["understood"] == SAMPLE_EXPLANATION["understood"]
    assert payload["approach"] == SAMPLE_EXPLANATION["approach"]
    assert payload["assumptions"] == SAMPLE_EXPLANATION["assumptions"]
    # The record half: a real read, not prose about a read.
    paths = [r["path"] for r in payload["used"]["reads"]]
    assert "README.md" in paths
    assert payload["version"] == 1
    # And it is carried on the result too, for a consumer that never watches the stream.
    assert res.explanation == payload


def test_small_talk_turn_emits_no_explanation_and_makes_no_explain_call():
    """The gate is model-free, so an ineligible turn costs nothing at all."""
    provider = ExplainingProvider(
        decisions=[{"action": "answer", "rationale": "just a greeting"}],
        explanation=SAMPLE_EXPLANATION)
    sink = RecordingSink()
    o = orch(provider, config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1))
    o.run("hi", mode=Mode.LIVE, sink=sink)

    assert EVENT_EXPLANATION not in sink.types()
    assert provider.explain_prompts == [], "an ineligible turn must not reach the model"


def test_flag_off_emits_nothing():
    provider = ExplainingProvider(
        decisions=[{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look"},
                   {"action": "answer", "rationale": "have it"}],
        explanation=SAMPLE_EXPLANATION)
    sink = RecordingSink()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(verify_claims=False, answer_goal_max_iterations=1))
    res = o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=sink)

    assert EVENT_EXPLANATION not in sink.types()
    assert res.explanation is None
    assert provider.explain_prompts == []


# --- 3. the verifiable half survives a failed generation ---------------------------------------

def test_a_failed_explain_call_still_yields_the_recorded_half():
    provider = ExplainRaisingProvider(
        decisions=[{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look"},
                   {"action": "answer", "rationale": "have it"}])
    sink = RecordingSink()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1))
    o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=sink)

    events = sink.of_type(EVENT_EXPLANATION)
    assert events, "a failed generation must not cost the recorded half of the panel"
    payload = events[0]["data"]
    assert "understood" not in payload
    assert [r["path"] for r in payload["used"]["reads"]] == ["README.md"]


# --- 4. the record block the generation call is constrained to -------------------------------

def test_record_block_states_plainly_when_nothing_ran():
    """The empty case is where a model is most tempted to invent a process, so it is spelled out."""
    block = render_record_for_prompt(TurnTrace(kind="answer", answer="Sure.", steps=1))
    assert "Nothing was read, searched or looked up this turn." in block
    assert "No action was executed this turn" in block


def test_record_block_reports_a_failed_action_as_failed():
    trace = TurnTrace(kind="answer", answer="Tried.", steps=2,
                      actions=[{"goal": "send the email", "succeeded": False, "failed": True,
                                "error": "smtp refused"}])
    block = render_record_for_prompt(trace)
    assert "FAILED" in block and "smtp refused" in block
    assert render_used(trace)["actions"] == [{"goal": "send the email", "state": "failed"}]


def test_record_block_flags_a_claim_the_record_does_not_back():
    trace = TurnTrace(kind="answer", answer="Saved it.", steps=2,
                      goal_verdict={"met": False, "reason": "no save recorded",
                                    "claims_unexecuted": True})
    block = render_record_for_prompt(trace)
    assert "WARNING" in block
    assert build_payload(trace, None)["signals"]["claims_unexecuted"] is True


def test_payload_signals_carry_the_real_verdict():
    trace = TurnTrace(kind="answer", answer="Yes.", steps=2, exit_reason="verified",
                      goal_verdict={"met": True, "reason": "the document says so"},
                      gathered=[{"kind": "read", "rel_path": "README.md"}])
    signals = build_payload(trace, None)["signals"]
    assert signals["goal_met"] is True
    assert signals["verdict_reason"] == "the document says so"
    assert signals["exit_reason"] == "verified"
    assert signals["steps"] == 2
