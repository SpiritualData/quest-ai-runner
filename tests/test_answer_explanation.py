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
5. THE METRICS ARE FACTS (version 3). The metric before and after reaches the payload from the
   runner's ledger alone: a model that writes different numbers in every field changes nothing in
   ``metrics``, the facts survive a failed or impossible generation call, and a turn with no metric
   results gets the payload (and the prompt) it always got.

Offline, no network.
"""
from typing import Any, Dict, List

import pytest

from quest_ai_runner.core.adapters import EVENT_DONE, EVENT_EXPLANATION, EVENT_RESULT, DeepResult
from quest_ai_runner.core.answer_explanation import (
    EXPLAIN_PROMPT,
    EXPLANATION_VERSION,
    MAX_ITEMS_PER_KIND,
    METRIC_KEYS,
    METRIC_RULE,
    TurnTrace,
    build_payload,
    has_renderable_content,
    is_eligible,
    metric_rule,
    render_metrics,
    render_record_for_prompt,
    render_used,
    trace_from_result,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Mode, Orchestrator, OrchestratorConfig, OrchestratorResult
from quest_ai_runner.core import StreamSink

from .conftest import StubProvider, StubRetrieval
from .test_goal_metrics_flow import TASK, Worker, deep_plan, world  # noqa: F401  (world is a fixture)
from .test_per_goal_context_iteration import ScriptedProvider


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
    "evolvable_structure": {"ideal": "You can act on the metrics policy without guessing.",
                            "metric": "The figures you report match the weekly count.",
                            "structure": ""},
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
    assert payload["version"] == 3
    # The ESOI section: empty sentences are dropped, the rest kept as written.
    assert payload["evolvable_structure"] == {
        "ideal": "You can act on the metrics policy without guessing.",
        "metric": "The figures you report match the weekly count."}
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


# --- version 2: the evolvable structure section ------------------------------------------------

def test_evolvable_structure_is_optional_and_cleaned():
    """Absent, empty or malformed, it leaves the version 1 keys exactly as they were."""
    trace = TurnTrace(kind="answer", answer="Yes.", steps=2,
                      gathered=[{"kind": "read", "rel_path": "README.md"}])
    base = {"understood": "u", "approach": "a", "confidence": "c"}
    for raw in (None, {}, "a string", {"ideal": "  ", "metric": None}):
        payload = build_payload(trace, {**base, "evolvable_structure": raw})
        assert "evolvable_structure" not in payload
        assert payload["understood"] == "u"
    payload = build_payload(trace, {**base, "evolvable_structure": {
        "ideal": " Serve the plan. ", "structure": "Built on your weekly plan.", "other": "dropped"}})
    assert payload["evolvable_structure"] == {"ideal": "Serve the plan.",
                                              "structure": "Built on your weekly plan."}


def test_evolvable_structure_alone_is_renderable():
    from quest_ai_runner.core.answer_explanation import has_renderable_content
    assert has_renderable_content({"used": {}, "evolvable_structure": {"ideal": "x"}})


def test_a_deep_turn_carries_its_own_verifier_outcome():
    """A deep turn has no goal verdict; its exit reason already records the deep verifier's call."""
    for exit_reason, met in (("deep_met", True), ("deep_not_met", False), ("deferred", None)):
        trace = TurnTrace(kind="deep", answer="Done.", steps=1, exit_reason=exit_reason)
        assert build_payload(trace, None)["signals"]["goal_met"] is met
    # A real verdict still wins over the exit reason.
    trace = TurnTrace(kind="deep", answer="Done.", exit_reason="deep_met", goal_verdict={"met": False})
    assert build_payload(trace, None)["signals"]["goal_met"] is False


# --- version 3: the metric before and after are facts, written by code -----------------------------

MEASURED = {"solution_id": "greet-a-person", "metric_id": "greeting-pass-rate", "name": "Greeting pass rate",
            "milestone": "Share of sample names greeted in the standard form", "before": 0.5, "after": 0.75,
            "delta": 0.25, "source": "measured", "reason": "measured by offline_arm", "disagrees": False}
RECORDED = {**MEASURED, "before": 0.5, "after": 0.9, "delta": 0.4, "source": "recorded", "reason": "",
            "disagrees": True}
NOT_APPLICABLE = {"solution_id": "", "metric_id": "", "name": "", "milestone": "", "before": None, "after": None,
                  "delta": None, "source": "not_applicable", "reason": "Only a draft was edited.",
                  "disagrees": False}

# What a turn with no metric results produced at version 2, for the trace below (taken from the version 2 code).
V2_GOLDEN = {
    "version": 2,
    "used": {"cards": [{"title": "Notes card", "adapter": "files"}],
             "sources": [{"label": "Corpus files", "adapter": "files", "item_count": 3}],
             "reads": [{"kind": "read", "path": "notes.md", "locator": "head"}],
             "actions": [{"goal": "tidy the notes", "state": "succeeded"}], "web": False},
    "signals": {"exit_reason": "deep_met", "goal_met": True, "verdict_reason": "", "claims_unexecuted": False,
                "steps": 2, "deep": True},
    "understood": "You wanted the notes tidied.",
    "approach": "Edited the one file.",
    "confidence": "High, the edit was recorded.",
    "assumptions": ["The notes were the right file."],
    "limitations": ["Only one file."],
    "what_would_change": ["A second notes file."],
    "evolvable_structure": {"ideal": "Notes you can scan.", "metric": "Time to find an item."},
}
V2_RECORD = (
    "Looked at 1 thing(s):\n  - read: notes.md (head)\n"
    "Background context available while answering: Notes card; Corpus files\n"
    "Actions attempted:\n  - tidy the notes: SUCCEEDED\n"
    "Planning steps taken: 2.")


def notes_trace(**extra) -> TurnTrace:
    return TurnTrace(
        kind="deep", user_message="Tidy the notes", goal_condition="Notes are tidy", answer="Tidied the notes.",
        steps=2, exit_reason="deep_met", gathered=[{"kind": "read", "rel_path": "notes.md", "locator": "head"}],
        actions=[{"goal": "tidy the notes", "succeeded": True, "failed": False}],
        cards=[{"title": "Notes card", "adapter": "files"}],
        sources=[{"label": "Corpus files", "adapter": "files", "item_count": 3}], **extra)


WRITTEN_V2 = {"understood": "You wanted the notes tidied.", "approach": "Edited the one file.",
              "assumptions": ["The notes were the right file."], "confidence": "High, the edit was recorded.",
              "limitations": ["Only one file."], "what_would_change": ["A second notes file."],
              "evolvable_structure": {"ideal": "Notes you can scan.", "metric": "Time to find an item.",
                                      "structure": ""}}

# Wrong in every field, the metric figures included: the numbers a model might make up.
LYING_EXPLANATION = {
    "understood": "You wanted the pass rate to reach 0.99.", "approach": "The pass rate went from 0.1 to 0.99.",
    "assumptions": ["The pass rate was 0.1 before."], "confidence": "Very high: the metric is now 0.99.",
    "limitations": ["Pass rate 0.99 was never re-measured."], "what_would_change": ["A pass rate below 0.99."],
    "evolvable_structure": {"ideal": "Reach a pass rate of 0.99.", "metric": "Pass rate rose from 0.1 to 0.99.",
                            "structure": "A pass rate of 0.99."},
    "metrics": [{**MEASURED, "before": 0.1, "after": 0.99, "delta": 0.89}], "metric_missing": True,
}


def test_the_version_is_3_and_a_turn_without_metrics_keeps_its_version_2_shape():
    """Additive: every version 2 key means what it did, and no new key appears when there is nothing to put in it."""
    assert EXPLANATION_VERSION == 3
    payload = build_payload(notes_trace(), WRITTEN_V2)
    assert payload["version"] == 3
    assert {**payload, "version": 2} == V2_GOLDEN
    assert "metrics" not in payload and "metric_missing" not in payload
    assert render_record_for_prompt(notes_trace()) == V2_RECORD
    assert build_payload(notes_trace(), None)["used"] == V2_GOLDEN["used"]


def test_a_turn_without_metrics_gets_the_prompt_it_always_got():
    trace = notes_trace()
    assert metric_rule(trace) == ""
    prompt = EXPLAIN_PROMPT.format(user_message="u", goal_condition="g", answer="a", record=V2_RECORD,
                                   metric_rule=metric_rule(trace), language="\n\nL")
    assert "Metric results" not in prompt and "metric" not in V2_RECORD.lower()
    assert prompt.endswith("or parentheses.\n\n\nL"), "the rule's slot adds nothing when it is empty"


def test_the_payload_carries_the_metrics_of_a_scripted_result():
    result = OrchestratorResult(kind="deep", exit_reason="deep_met", deep_results=[DeepResult(met=True, output="Done.")],
                                metric_results=[MEASURED, RECORDED, NOT_APPLICABLE], metric_missing=False)
    trace = trace_from_result(result, user_message="Improve the greeting", goal_condition="Greetings by name")
    payload = build_payload(trace, None)
    assert payload["version"] == 3
    assert payload["metrics"] == [MEASURED, RECORDED, NOT_APPLICABLE]
    assert all(tuple(entry) == METRIC_KEYS for entry in payload["metrics"])
    assert "metric_missing" not in payload
    assert is_eligible(trace) and has_renderable_content(payload)


def test_a_change_with_no_metric_recorded_travels_as_a_flag_with_no_entries():
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(met=True, output="Done.")],
                                metric_results=[], metric_missing=True)
    payload = build_payload(trace_from_result(result, user_message="m", goal_condition="g"), None)
    assert payload["metric_missing"] is True and "metrics" not in payload
    assert has_renderable_content(payload)


def test_a_turn_with_metric_results_is_eligible_even_when_nothing_else_would_make_it_so():
    bare = TurnTrace(kind="answer", answer="Done.", steps=1)
    assert is_eligible(bare) is False
    assert is_eligible(TurnTrace(kind="answer", answer="Done.", steps=1, metrics=[MEASURED])) is True
    assert is_eligible(TurnTrace(kind="answer", answer="Done.", steps=1, metric_missing=True)) is True
    # What is not an answer stays not an answer, and an entry nobody could vouch for is not a fact.
    assert is_eligible(TurnTrace(kind="confirm", answer="", steps=1, metrics=[MEASURED])) is False
    assert is_eligible(TurnTrace(kind="answer", answer="", steps=1, metrics=[MEASURED])) is False
    assert is_eligible(TurnTrace(kind="answer", answer="Done.", steps=1, metrics=[{**MEASURED, "source": "guess"}])) is False


def test_the_metric_section_is_cleaned_and_capped_without_a_model():
    many = [{**MEASURED, "metric_id": f"m{i}"} for i in range(MAX_ITEMS_PER_KIND + 5)]
    section = render_metrics(TurnTrace(metrics=many))
    assert len(section["metrics"]) == MAX_ITEMS_PER_KIND
    odd = {**MEASURED, "before": "0.4", "after": float("nan"), "delta": True, "extra": "dropped", "disagrees": "yes"}
    cleaned = render_metrics(TurnTrace(metrics=[odd, "not a dict", None, {**MEASURED, "source": "guess"}]))["metrics"]
    assert len(cleaned) == 1
    assert (cleaned[0]["before"], cleaned[0]["after"], cleaned[0]["delta"]) == (None, None, None)
    assert cleaned[0]["disagrees"] is False and "extra" not in cleaned[0] and tuple(cleaned[0]) == METRIC_KEYS
    assert render_metrics(TurnTrace()) == {}


def test_a_model_that_writes_wrong_numbers_in_every_field_changes_nothing_in_the_metrics():
    trace = notes_trace(metrics=[MEASURED, RECORDED], metric_missing=False)
    honest = build_payload(trace, WRITTEN_V2)
    lying = build_payload(trace, LYING_EXPLANATION)
    assert lying["metrics"] == honest["metrics"] == [MEASURED, RECORDED]
    assert "metric_missing" not in lying, "a flag the model wrote is never read"
    assert lying["used"] == honest["used"] and lying["signals"] == honest["signals"]


def test_the_record_carries_the_metric_values_and_the_prompt_forbids_stating_them():
    trace = notes_trace(metrics=[MEASURED, RECORDED, NOT_APPLICABLE], metric_missing=True)
    record = render_record_for_prompt(trace)
    assert record.splitlines()[0].startswith("Metric results the runner's code kept")
    assert "Greeting pass rate: before 0.5, after 0.75 (measured by the runner's own meter)" in record
    assert "before 0.5, after 0.9 (recorded by the AI, not checked; the AI's record and the runner's reading differ)" in record
    assert "no metric applies: Only a draft was edited." in record
    assert "No metric result was recorded for this change." in record
    assert record.endswith("Planning steps taken: 2.")
    prompt = EXPLAIN_PROMPT.format(user_message="u", goal_condition="g", answer="a", record=record,
                                   metric_rule=metric_rule(trace), language="\n\nL")
    assert METRIC_RULE in prompt and prompt.count("Metric results:") == 1
    assert "—" not in METRIC_RULE and "—" not in record and "—" not in prompt


def test_the_metric_facts_come_first_in_the_record_so_a_long_record_loses_its_tail_not_them():
    reads = [{"kind": "read", "rel_path": "f%d.md" % i, "locator": "head" + "x" * 80} for i in range(MAX_ITEMS_PER_KIND)]
    record = render_record_for_prompt(TurnTrace(kind="deep", answer="Done.", steps=1, gathered=reads, metrics=[MEASURED]))
    assert "Greeting pass rate: before 0.5, after 0.75" in record[:400]


def test_has_renderable_content_is_unchanged_for_version_2_payloads_and_counts_the_new_keys():
    assert not has_renderable_content({"used": {}})
    assert not has_renderable_content({"used": {}, "metrics": [], "signals": {"steps": 1}})
    assert has_renderable_content({"used": {"web": True}})
    assert has_renderable_content({"used": {}, "confidence": "High."})
    assert has_renderable_content({"used": {}, "metrics": [MEASURED]})
    assert has_renderable_content({"used": {}, "metric_missing": True})
    assert not has_renderable_content("not a payload")


# --- the metrics through the real turn: a deep goal, the ledger, the event, the result -------------

class ExplainScript(ScriptedProvider):
    """Plans and verdicts as scripted, and answers the explanation call with ``explanation`` (or raises)."""

    def __init__(self, plans, verdicts, explanation=None, fail=False):
        super().__init__(plans, verdicts)
        self.explanation, self.fail, self.explain_prompts = explanation, fail, []

    def plan(self, prompt, *, model, tool_schema):
        if (tool_schema or {}).get("name") == "answer_explanation":
            self.explain_prompts.append(prompt)
            if self.fail:
                raise RuntimeError("explain provider blew up")
            return dict(self.explanation or {})
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def deep_turn(world, provider, steps, **config):
    worker = Worker(world.registry, steps)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=worker, tools=world.registry,
                        config=OrchestratorConfig(explain_answer=True, **config))
    sink = RecordingSink()
    result = orch.run("please do the thing", mode=Mode.LIVE, sink=sink, context_meta={"task_id": TASK})
    return orch, result, sink


RECORD_CALL = ("record", {"metric_id": "metric-x", "before": 0.25, "after": 0.5})
LEDGER_ENTRY = {"solution_id": "", "metric_id": "metric-x", "name": "", "milestone": "", "before": 0.25, "after": 0.5,
                "delta": 0.25, "source": "recorded", "reason": "", "disagrees": False}


def test_a_finished_deep_goal_puts_its_ledger_values_in_the_explanation_with_a_lying_model(world):
    provider = ExplainScript([deep_plan()], [{"met": True}], LYING_EXPLANATION)
    orch, result, sink = deep_turn(world, provider, [["change", RECORD_CALL]])
    payload = sink.of_type(EVENT_EXPLANATION)[0]["data"]
    assert payload["metrics"] == [LEDGER_ENTRY] == result.metric_results
    assert "metric_missing" not in payload and result.explanation == payload
    assert payload["version"] == 3
    # The lie reached the prose half (the prompt only asks), and nowhere near the facts.
    assert payload["evolvable_structure"]["metric"] == "Pass rate rose from 0.1 to 0.99."
    assert METRIC_RULE in provider.explain_prompts[0] and "metric-x: before 0.25, after 0.5" in provider.explain_prompts[0]
    types = sink.types()
    assert types.index(EVENT_RESULT) < types.index(EVENT_EXPLANATION) < types.index(EVENT_DONE)


def test_the_metric_facts_survive_a_failed_generation_call_and_cost_no_extra_call(world):
    provider = ExplainScript([deep_plan()], [{"met": True}], fail=True)
    orch, result, sink = deep_turn(world, provider, [["change", RECORD_CALL]])
    payload = sink.of_type(EVENT_EXPLANATION)[0]["data"]
    assert payload["metrics"] == [LEDGER_ENTRY]
    assert "understood" not in payload and "approach" not in payload
    assert len(provider.explain_prompts) == 1, "the one generation call that failed, and no other"


def test_the_metric_facts_survive_a_generation_call_that_cannot_run(world):
    provider = ExplainScript([deep_plan()], [{"met": True}], LYING_EXPLANATION)
    worker = Worker(world.registry, [["change", RECORD_CALL]])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=worker, tools=world.registry, config=OrchestratorConfig(explain_answer=True))
    orch.registry.resolve_tier = lambda tier: ""   # no model for the written half
    sink = RecordingSink()
    orch.run("please do the thing", mode=Mode.LIVE, sink=sink, context_meta={"task_id": TASK})
    payload = sink.of_type(EVENT_EXPLANATION)[0]["data"]
    assert payload["metrics"] == [LEDGER_ENTRY] and "understood" not in payload
    assert provider.explain_prompts == []


def test_a_change_that_ended_with_no_metric_recorded_is_a_flag_in_the_explanation(world):
    provider = ExplainScript([deep_plan()], [{"met": True}], fail=True)
    orch, result, sink = deep_turn(world, provider, [["change"], []])
    payload = sink.of_type(EVENT_EXPLANATION)[0]["data"]
    assert payload["metric_missing"] is True and "metrics" not in payload
    assert result.metric_missing is True and result.explanation == payload


def test_the_flag_off_leaves_a_turn_with_metrics_exactly_as_it_was(world):
    provider = ExplainScript([deep_plan()], [{"met": True}], LYING_EXPLANATION)
    worker = Worker(world.registry, [["change", RECORD_CALL]])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=worker, tools=world.registry, config=OrchestratorConfig())
    sink = RecordingSink()
    result = orch.run("please do the thing", mode=Mode.LIVE, sink=sink, context_meta={"task_id": TASK})
    assert EVENT_EXPLANATION not in sink.types() and result.explanation is None and provider.explain_prompts == []
    assert result.metric_results == [LEDGER_ENTRY], "the result and the events still carry them"
