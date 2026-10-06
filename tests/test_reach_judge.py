"""The REACH JUDGE: one small question, answered by a stronger tier, before planning.

Pinned here:

  * normalize_verdict's fail-open shape: a bad response is None, never a guess, and covered_by's
    null-like spellings collapse to None;
  * parse_judge_text handles a bare JSON string and a markdown-fenced one, and gives up cleanly on
    prose or malformed JSON;
  * verdict_block adds EXACTLY nothing for the common "inside" case (and for no verdict at all),
    and names the right instruction for "outside" (covered and uncovered) and "world";
  * judge_prompt carries the reach summary and the message, truncating an over-long message;
  * Orchestrator.reach_verdict: off by default, inert without a read_reach_summary, caches its
    answer for the turn, fails open on a raised exception or a garbage answer, and passes a `tier`
    kwarg only to a provider whose plan() accepts one;
  * Orchestrator._plan appends the verdict text to the prompt it actually sends for "outside", and
    sends no verdict heading at all for "inside".

Fully offline: every provider here is a fake that returns a scripted value. No network call and no
real LLM call is made anywhere in this file.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    provider_call_accepts_tier,
)
from quest_ai_runner.core.reach_judge import (
    REACH_VERDICTS,
    VERDICT_HEADING,
    judge_prompt,
    normalize_verdict,
    parse_judge_text,
    verdict_block,
)

from .conftest import StubRetrieval


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class RecordingProvider:
    """A ModelProvider whose plan() records every call and replays one scripted response.

    Accepts an explicit ``tier`` kwarg, matching a current adapter's shape.
    """

    def __init__(self, response: Any = None, raises: Optional[BaseException] = None):
        self.response = response
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             tier: Optional[str] = None) -> Any:
        self.calls.append({"prompt": prompt, "model": model, "tool_schema": tool_schema,
                           "tier": tier})
        if self.raises is not None:
            raise self.raises
        return self.response

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class NoTierProvider:
    """A ModelProvider whose plan() has no ``tier`` parameter, matching an older adapter."""

    def __init__(self, response: Any = None):
        self.response = response
        self.calls: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Any:
        self.calls.append({"prompt": prompt, "model": model, "tool_schema": tool_schema})
        return self.response

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class DispatchingProvider:
    """Answers the reach judge's call one way and the main planner's call another.

    Distinguishes the two calls by the tool schema's name ("reach" versus everything else), the
    same way the orchestrator itself distinguishes them.
    """

    def __init__(self, reach_response: Any, decide_response: Any):
        self.reach_response = reach_response
        self.decide_response = decide_response
        self.prompts: List[str] = []
        self.schemas: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             tier: Optional[str] = None) -> Any:
        self.prompts.append(prompt)
        self.schemas.append(tool_schema)
        if tool_schema.get("name") == "reach":
            return self.reach_response
        return self.decide_response

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


def build(provider: Any, **cfg: Any) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(**cfg))


# ---------------------------------------------------------------------------
# normalize_verdict
# ---------------------------------------------------------------------------

def test_normalize_verdict_accepts_valid_verdicts_any_case_with_whitespace():
    for v in REACH_VERDICTS:
        assert normalize_verdict({"reach": f"  {v.upper()}  "}) == {"reach": v, "covered_by": None}


def test_normalize_verdict_rejects_a_non_dict():
    assert normalize_verdict("inside") is None
    assert normalize_verdict(None) is None
    assert normalize_verdict(["inside"]) is None
    assert normalize_verdict(42) is None


def test_normalize_verdict_rejects_a_missing_verdict():
    assert normalize_verdict({}) is None
    assert normalize_verdict({"covered_by": "something"}) is None


def test_normalize_verdict_rejects_a_hallucinated_verdict():
    assert normalize_verdict({"reach": "maybe"}) is None
    assert normalize_verdict({"reach": "elsewhere"}) is None


def test_normalize_verdict_rejects_a_wrong_type():
    assert normalize_verdict({"reach": 1}) is None
    assert normalize_verdict({"reach": ["outside"]}) is None
    assert normalize_verdict({"reach": None}) is None


def test_normalize_verdict_maps_null_like_covered_by_strings_to_none():
    for spelling in ("null", "none", "n/a", "NULL", "None", "N/A"):
        result = normalize_verdict({"reach": "outside", "covered_by": spelling})
        assert result["covered_by"] is None, spelling


def test_normalize_verdict_strips_covered_by():
    result = normalize_verdict({"reach": "outside", "covered_by": "  a-local-service  "})
    assert result == {"reach": "outside", "covered_by": "a-local-service"}


# ---------------------------------------------------------------------------
# parse_judge_text
# ---------------------------------------------------------------------------

def test_parse_judge_text_parses_a_bare_json_string():
    assert parse_judge_text('{"reach": "world"}') == {"reach": "world", "covered_by": None}


def test_parse_judge_text_parses_a_markdown_fenced_json_string():
    text = "```json\n{\"reach\": \"outside\", \"covered_by\": \"a-local-service\"}\n```"
    assert parse_judge_text(text) == {"reach": "outside", "covered_by": "a-local-service"}


def test_parse_judge_text_returns_none_for_prose():
    assert parse_judge_text("Sure, happy to help with that request.") is None


def test_parse_judge_text_returns_none_for_malformed_json():
    assert parse_judge_text('{"reach": "world"') is None
    assert parse_judge_text("") is None


def test_parse_judge_text_also_accepts_a_dict_directly():
    assert parse_judge_text({"reach": "inside"}) == {"reach": "inside", "covered_by": None}


def test_parse_judge_text_returns_none_for_other_types():
    assert parse_judge_text(42) is None
    assert parse_judge_text(None) is None


# ---------------------------------------------------------------------------
# verdict_block
# ---------------------------------------------------------------------------

def test_verdict_block_is_exactly_empty_for_inside():
    assert verdict_block({"reach": "inside", "covered_by": None}) == ""


def test_verdict_block_is_exactly_empty_for_none():
    assert verdict_block(None) == ""


def test_verdict_block_outside_covered_names_the_environment_and_says_hand_it_off():
    block = verdict_block({"reach": "outside", "covered_by": "a-local-service"})
    assert block.startswith(VERDICT_HEADING)
    assert "a-local-service" in block
    assert "Hand it off" in block


def test_verdict_block_outside_uncovered_says_hand_nothing_off():
    block = verdict_block({"reach": "outside", "covered_by": None})
    assert block.startswith(VERDICT_HEADING)
    assert "do not hand it off to anything" in block
    assert "Answer, and say plainly" in block


def test_verdict_block_world_tells_the_planner_to_answer():
    block = verdict_block({"reach": "world", "covered_by": None})
    assert block.startswith(VERDICT_HEADING)
    assert "answer it" in block.lower()
    assert "Do not hand it off" in block


# ---------------------------------------------------------------------------
# judge_prompt
# ---------------------------------------------------------------------------

def test_judge_prompt_includes_the_reach_summary_and_the_message():
    prompt = judge_prompt("please check on the deploy", "Can read local project notes.")
    assert "please check on the deploy" in prompt
    assert "Can read local project notes." in prompt


def test_judge_prompt_truncates_an_over_long_message():
    message = ("a" * 20) + "TAIL_THAT_SHOULD_BE_CUT"
    prompt = judge_prompt(message, "a short summary", max_message_chars=20)
    assert "a" * 20 in prompt
    assert "TAIL_THAT_SHOULD_BE_CUT" not in prompt


# ---------------------------------------------------------------------------
# Orchestrator.reach_verdict
# ---------------------------------------------------------------------------

def test_reach_verdict_returns_none_and_makes_no_call_when_judge_is_off():
    provider = RecordingProvider(response={"reach": "inside"})
    orch = build(provider, planner_reach_judge=False, read_reach_summary="Can read local notes.")
    assert orch.reach_verdict("restart the thing") is None
    assert provider.calls == []


def test_reach_verdict_returns_none_and_makes_no_call_when_summary_is_blank():
    provider = RecordingProvider(response={"reach": "inside"})
    for summary in ("", "   ", "\n\t"):
        orch = build(provider, planner_reach_judge=True, read_reach_summary=summary)
        assert orch.reach_verdict("restart the thing") is None
    assert provider.calls == []


def test_reach_verdict_normalizes_the_answer_and_caches_it_for_the_turn():
    provider = RecordingProvider(response={"reach": "OUTSIDE", "covered_by": " a-local-service "})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    first = orch.reach_verdict("restart the service")
    assert first == {"reach": "outside", "covered_by": "a-local-service"}
    second = orch.reach_verdict("restart the service")
    assert second == first
    assert len(provider.calls) == 1


def test_reach_verdict_returns_none_and_does_not_take_the_turn_down_when_the_provider_raises():
    provider = RecordingProvider(raises=RuntimeError("boom"))
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    assert orch.reach_verdict("restart the service") is None


def test_reach_verdict_returns_none_when_the_provider_answers_garbage():
    provider = RecordingProvider(response="sure, here is a plain sentence")
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    assert orch.reach_verdict("restart the service") is None
    provider_two = RecordingProvider(response={"reach": "sideways"})
    orch_two = build(provider_two, planner_reach_judge=True,
                     read_reach_summary="Can read local notes.")
    assert orch_two.reach_verdict("restart the service") is None


def test_reach_verdict_passes_tier_to_a_provider_whose_plan_accepts_it():
    provider = RecordingProvider(response={"reach": "inside"})
    assert provider_call_accepts_tier(provider.plan) is True
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.",
                planner_reach_judge_tier="best")
    orch.reach_verdict("restart the service")
    assert provider.calls[0]["tier"] == "best"


def test_reach_verdict_does_not_pass_tier_to_a_provider_whose_plan_does_not_accept_it():
    provider = NoTierProvider(response={"reach": "inside"})
    assert provider_call_accepts_tier(provider.plan) is False
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    # If the orchestrator tried to pass `tier` here it would hit a TypeError, which reach_verdict
    # swallows into None. Getting the real verdict back proves `tier` was never offered.
    verdict = orch.reach_verdict("restart the service")
    assert verdict == {"reach": "inside", "covered_by": None}
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Orchestrator._plan: the verdict actually reaches the prompt (or does not)
# ---------------------------------------------------------------------------

def test_plan_appends_the_verdict_text_to_the_prompt_for_an_outside_verdict():
    provider = DispatchingProvider(
        reach_response={"reach": "outside", "covered_by": "a-local-service"},
        decide_response={"action": "answer", "rationale": "ok"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    orch._plan("restart the service", "", "", [])
    main_prompt = provider.prompts[-1]
    assert VERDICT_HEADING in main_prompt
    assert "a-local-service" in main_prompt


def test_plan_sends_no_verdict_heading_at_all_for_an_inside_verdict():
    provider = DispatchingProvider(
        reach_response={"reach": "inside"},
        decide_response={"action": "answer", "rationale": "ok"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    orch._plan("what is still open on my list", "", "", [])
    main_prompt = provider.prompts[-1]
    assert VERDICT_HEADING not in main_prompt


# ---------------------------------------------------------------------------
# The judge overlaps the turn instead of sitting in front of the first plan
# ---------------------------------------------------------------------------

class SlowJudgeProvider(DispatchingProvider):
    """Sleeps on the judge's call only, and records when the judge started."""

    def __init__(self, delay: float, **kwargs: Any):
        super().__init__(**kwargs)
        self.delay = delay
        self.judge_started = threading.Event()
        self.judge_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             tier: Optional[str] = None) -> Any:
        if tool_schema.get("name") == "reach":
            self.judge_calls += 1
            self.judge_started.set()
            time.sleep(self.delay)
        return super().plan(prompt, model=model, tool_schema=tool_schema, tier=tier)


def test_prefetch_returns_the_same_future_for_the_same_request_and_judges_once():
    provider = SlowJudgeProvider(0.0, reach_response={"reach": "world"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    first = orch.prefetch_reach_verdict("what is the weather today")
    assert first is orch.prefetch_reach_verdict("what is the weather today")
    assert orch.reach_verdict("what is the weather today") == {"reach": "world", "covered_by": None}
    assert provider.judge_calls == 1


def test_prefetch_is_a_no_op_when_the_judge_is_off():
    provider = SlowJudgeProvider(0.0, reach_response={"reach": "world"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=False, read_reach_summary="Can read local notes.")
    assert orch.prefetch_reach_verdict("anything") is None
    assert provider.judge_calls == 0


def test_a_prefetched_verdict_costs_the_first_plan_no_wait():
    provider = SlowJudgeProvider(0.3, reach_response={"reach": "world"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    orch.prefetch_reach_verdict("who won last night")
    time.sleep(0.4)  # stands in for request understanding and context assembly
    started = time.monotonic()
    orch._plan("who won last night", "", "", [])
    assert time.monotonic() - started < 0.2
    assert provider.judge_calls == 1


def test_a_judge_that_overruns_its_timeout_leaves_the_plan_without_a_verdict():
    provider = SlowJudgeProvider(1.0, reach_response={"reach": "outside", "covered_by": "x"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.",
                 planner_reach_judge_timeout_seconds=0.05)
    started = time.monotonic()
    assert orch.reach_verdict("restart the service") is None
    assert time.monotonic() - started < 0.5


def test_the_verdict_cache_is_bounded():
    provider = SlowJudgeProvider(0.0, reach_response={"reach": "inside"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.")
    orch.REACH_VERDICT_CACHE_SIZE = 3
    for i in range(10):
        orch.reach_verdict(f"request number {i}")
    assert len(orch.reach_verdict_cache) == 3
    assert list(orch.reach_verdict_cache) == [f"request number {i}"[:500] for i in (7, 8, 9)]


class WaitingAssembler:
    """A context assembler that records whether the judge had already started while it ran."""

    def __init__(self, provider: SlowJudgeProvider):
        self.provider = provider
        self.judge_running_during_assembly: Optional[bool] = None

    def assemble(self, message: str, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        self.judge_running_during_assembly = self.provider.judge_started.wait(timeout=2.0)
        return AssembledContext(context_view="")


def test_run_starts_the_judge_before_context_assembly_finishes():
    provider = SlowJudgeProvider(0.0, reach_response={"reach": "inside"},
                                 decide_response={"action": "answer", "rationale": "ok"})
    assembler = WaitingAssembler(provider)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=assembler,
                        config=OrchestratorConfig(planner_reach_judge=True,
                                                  read_reach_summary="Can read local notes."))
    orch.run("what is still open on my list")
    # Serial (the old shape), the judge could only start at the first plan, after assembly.
    assert assembler.judge_running_during_assembly is True
    assert provider.judge_calls == 1


def test_a_timed_out_judge_is_waited_on_once_per_turn_not_once_per_step():
    provider = SlowJudgeProvider(1.0, reach_response={"reach": "outside", "covered_by": "x"},
                                 decide_response={"action": "answer"})
    orch = build(provider, planner_reach_judge=True, read_reach_summary="Can read local notes.",
                 planner_reach_judge_timeout_seconds=0.2)
    assert orch.reach_verdict("restart the service") is None
    started = time.monotonic()
    assert orch.reach_verdict("restart the service") is None   # a re-plan step: no second wait
    assert time.monotonic() - started < 0.1
    assert provider.judge_calls == 1
