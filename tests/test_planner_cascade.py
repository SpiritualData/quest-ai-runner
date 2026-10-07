"""The OVERSEER CASCADE: re-decide a flagged routing decision on a stronger model, from a digest.

Pinned here:

  * escalation_spec/escalation_levels parse the operator's comma list of confidence levels and
    "action:<name>" entries, dropping anything unrecognised rather than raising;
  * should_escalate: an action match always escalates; a confidence match escalates only when
    levels were named AND the decision actually reported one (the fail-safe: no reported
    confidence means no confidence-based escalation, ever, even with levels named);
  * truncate_keep_both_ends keeps content from BOTH ends under a tight budget, never just one;
  * build_review_digest never drops the rubric or the decision, even at a tiny character budget,
    and stays under the budget when there is room to actually trim;
  * describe_decision renders each optional field only when the decision actually carries it;
  * apply_review: an agreeing reviewer changes nothing (same object back, so the cheap model's own
    reads are never dropped), a malformed or hallucinated verdict leaves the cheap decision
    standing, and each action the reviewer can choose produces the right shape, including the
    read-with-nothing-to-read degrade to a plain answer;
  * Orchestrator.cascade_review: off by default, silent when the signal does not match, exactly
    one call on the configured tier when it does, fails open on a raised exception, and tracks
    last_plan_cascaded / planner_cascade_reviews correctly in every case.

Fully offline: every provider here is a fake that returns a scripted value. No network call and no
real LLM call is made anywhere in this file.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import PlanDecision
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    PLANNER_DECISION_RUBRIC,
    provider_call_accepts_tier,
)
from quest_ai_runner.core.planner_cascade import (
    WEB_REVIEW_NOTE,
    apply_review,
    build_review_digest,
    describe_decision,
    escalation_levels,
    escalation_spec,
    should_escalate,
    truncate_keep_both_ends,
    user_goal_fallback,
)

from .conftest import StubRetrieval


# ---------------------------------------------------------------------------
# escalation_spec / escalation_levels
# ---------------------------------------------------------------------------

def test_escalation_spec_parses_confidence_levels():
    assert escalation_spec("low,medium") == {"levels": frozenset({"low", "medium"}),
                                              "actions": frozenset()}


def test_escalation_spec_parses_action_entries():
    assert escalation_spec("action:read,action:confirm") == {
        "levels": frozenset(), "actions": frozenset({"read", "confirm"})}


def test_escalation_spec_ignores_unknown_entries_and_empty_parts():
    spec = escalation_spec("low,,action:not_a_real_action,bananas,action:read")
    assert spec == {"levels": frozenset({"low"}), "actions": frozenset({"read"})}


def test_escalation_spec_is_case_insensitive():
    assert escalation_spec("LOW,Action:READ") == {"levels": frozenset({"low"}),
                                                   "actions": frozenset({"read"})}


def test_escalation_spec_handles_none_and_empty_string():
    assert escalation_spec(None) == {"levels": frozenset(), "actions": frozenset()}
    assert escalation_spec("") == {"levels": frozenset(), "actions": frozenset()}


def test_escalation_levels_returns_just_the_levels():
    assert escalation_levels("low,action:read,medium") == frozenset({"low", "medium"})


# ---------------------------------------------------------------------------
# should_escalate
# ---------------------------------------------------------------------------

def test_should_escalate_true_when_the_action_is_named():
    spec = escalation_spec("action:read")
    assert should_escalate(PlanDecision(action="read"), spec) is True


def test_should_escalate_true_when_confidence_is_named_and_reported():
    spec = escalation_spec("low")
    assert should_escalate(PlanDecision(action="answer", confidence="low"), spec) is True


def test_should_escalate_false_with_no_confidence_reported_even_with_levels_named():
    """The fail-safe: a model that omits the field must not escalate everything."""
    spec = escalation_spec("low,medium,high")
    assert should_escalate(PlanDecision(action="answer", confidence=None), spec) is False


def test_should_escalate_false_when_nothing_is_named():
    spec = escalation_spec("")
    assert should_escalate(PlanDecision(action="read", confidence="low"), spec) is False


def test_should_escalate_accepts_a_bare_set_of_levels_too():
    assert should_escalate(PlanDecision(action="answer", confidence="low"),
                           frozenset({"low"})) is True
    assert should_escalate(PlanDecision(action="answer", confidence=None),
                           frozenset({"low"})) is False


# ---------------------------------------------------------------------------
# truncate_keep_both_ends
# ---------------------------------------------------------------------------

def test_truncate_keep_both_ends_returns_unchanged_when_under_the_limit():
    text = "a short piece of text"
    assert truncate_keep_both_ends(text, 500) == text


def test_truncate_keep_both_ends_keeps_content_from_both_ends():
    text = "STARTMARKER" + ("x" * 1000) + "ENDMARKER"
    result = truncate_keep_both_ends(text, 80)
    assert "STARTMARKER" in result
    assert "ENDMARKER" in result
    assert len(result) <= 80


def test_truncate_keep_both_ends_respects_the_limit():
    text = "y" * 5000
    assert len(truncate_keep_both_ends(text, 200)) <= 200
    assert len(truncate_keep_both_ends(text, 0)) == 0


# ---------------------------------------------------------------------------
# build_review_digest
# ---------------------------------------------------------------------------

def test_build_review_digest_stays_under_max_chars_when_there_is_room_to_trim():
    decision = PlanDecision(action="read", reads=[{"grep": "x"}], rationale="because")
    digest = build_review_digest("do the thing", decision, PLANNER_DECISION_RUBRIC,
                                 context_view="z" * 50000, max_chars=6000)
    assert len(digest) <= 6000


def test_build_review_digest_always_contains_the_rubric_and_the_decision_at_a_tiny_budget():
    decision = PlanDecision(action="read", reads=[{"grep": "x"}], rationale="because")
    digest = build_review_digest("do the thing", decision, PLANNER_DECISION_RUBRIC,
                                 context_view="z" * 50000, max_chars=50)
    assert PLANNER_DECISION_RUBRIC in digest
    assert "action: read" in digest


def test_build_review_digest_includes_the_message():
    decision = PlanDecision(action="answer", rationale="ok")
    digest = build_review_digest("restart the deploy pipeline please", decision,
                                 PLANNER_DECISION_RUBRIC)
    assert "restart the deploy pipeline please" in digest


# Captured by running quest-ai-runner's core/planner_cascade.py AT ITS COMMIT PARENT TO bf609a8
# (`git show bf609a8^:quest_ai_runner/core/planner_cascade.py`), where build_review_digest() had
# no web_configured parameter at all, calling it with the exact same request/decision/rubric this
# test uses. This is the real pin: comparing the default call against an explicit
# web_configured=False call, both through the NEW code (what this test used to do), proves
# nothing about whether either one matches what the digest actually said before web_configured
# existed.
_PRE_CHANGE_REVIEW_DIGEST_NO_WEB = (
    'You are the OVERSEER of a cheaper model\'s routing decision. It has already decided; your job is to\nconfirm that decision or correct it, and nothing else. You do not answer the request and you do not\ndo the work.\n\nDECIDE IN THIS ORDER. Stop at the FIRST rule that applies; the doctrine below only refines it.\n  1. CURRENT FACTS ABOUT THE WORLD. A question about news, prices, weather, a score, a public\n     fact or anything else happening in the world right now is NOT work for a machine and NOT a\n     read of your own sources. Answer it: say what you reliably know, and say plainly that you\n     cannot check a live source for the current value if you cannot. Never hand such a question\n     to an execution environment, and never grep your sources for it.\n  2. OUT OF REACH. Does this need a place your reads cannot go: a machine and its files, folders,\n     processes, jobs or logs; a code repository; a spreadsheet, document or service held\n     elsewhere? Then do NOT read and do NOT run discovery first, and do not invent a `scope` or\n     `rel_path` for it: a server, repository, folder or spreadsheet NAME is never a source, not\n     even when the CONTEXT names it. If the CONTEXT names somewhere that covers that kind of\n     work, hand it off (answer + deferred_deep). If NOTHING there covers it, say so plainly and\n     hand nothing off. A read that already came back empty for such a request is this same\n     signal, not a reason to read again.\n  3. ALREADY RUNNING. Is this asking about work already in flight? Answer with that work\'s real\n     status from the records in CONTEXT. Never open a second run of the same thing.\n  4. QUESTION OR STATEMENT. Is this a question, or someone describing a plan, a preference or a\n     piece of context rather than instructing you to act now? Answer it, after reading real\n     content when it is about substance. An action word inside a question does not make it an\n     instruction.\n  5. INSTRUCTION TO ACT. It is a current instruction to produce or change something your deep\n     runner can reach: choose "deep" now, with no read first.\n  6. OTHERWISE. Read what you need, then answer.\n\n--- THE REQUEST ---\nrestart the deploy pipeline please\n\n--- THE GROUNDING THE DECIDER HAD (abridged) ---\n(none)\n\n--- WHAT IT OBSERVED SO FAR (empty means it has read nothing yet) ---\n[]\n\n--- ITS DECISION ---\naction: answer\nits reasoning: ok\n\nApply the rules above to the request. If the decision already follows them, return the SAME action\nand say so in one sentence. If it does not, return the action the rules require, and fill the few\nfields that action needs. The most common error to look for: work that lives outside what a read\ncan reach, sent to a read anyway, often with an invented path or scope. Correct that to a hand off\n("answer" with hand_off true) when the grounding names somewhere that covers the work, or to a\nplain "answer" that says nothing attached can reach it when it does not.\n'
)


def test_build_review_digest_is_byte_for_byte_unchanged_when_web_is_not_configured():
    decision = PlanDecision(action="answer", rationale="ok")
    default = build_review_digest("restart the deploy pipeline please", decision,
                                  PLANNER_DECISION_RUBRIC)
    assert default == _PRE_CHANGE_REVIEW_DIGEST_NO_WEB
    explicit_false = build_review_digest("restart the deploy pipeline please", decision,
                                         PLANNER_DECISION_RUBRIC, web_configured=False)
    assert explicit_false == _PRE_CHANGE_REVIEW_DIGEST_NO_WEB
    assert WEB_REVIEW_NOTE not in default


def test_build_review_digest_adds_the_web_note_when_web_is_configured():
    decision = PlanDecision(action="answer", rationale="ok")
    digest = build_review_digest("restart the deploy pipeline please", decision,
                                 PLANNER_DECISION_RUBRIC, web_configured=True)
    assert WEB_REVIEW_NOTE in digest
    assert "{\"web\":" in digest


# ---------------------------------------------------------------------------
# describe_decision
# ---------------------------------------------------------------------------

def test_describe_decision_includes_the_action():
    assert "action: read" in describe_decision(PlanDecision(action="read"))


def test_describe_decision_includes_confidence_only_when_present():
    assert "its own confidence: low" in describe_decision(
        PlanDecision(action="read", confidence="low"))
    assert "its own confidence" not in describe_decision(PlanDecision(action="read"))


def test_describe_decision_includes_reads_only_when_present():
    assert "reads it wants to run" in describe_decision(
        PlanDecision(action="read", reads=[{"grep": "pattern"}]))
    assert "reads it wants to run" not in describe_decision(PlanDecision(action="answer"))


def test_describe_decision_includes_goal_only_when_present():
    assert "goal: do the thing" in describe_decision(
        PlanDecision(action="deep", goal="do the thing"))
    assert "goal:" not in describe_decision(PlanDecision(action="deep"))


def test_describe_decision_includes_brief_only_when_present():
    assert "brief: do it carefully" in describe_decision(
        PlanDecision(action="deep", deep_brief="do it carefully"))
    assert "brief:" not in describe_decision(PlanDecision(action="deep"))


def test_describe_decision_includes_deferred_goal_only_when_present():
    assert "it also wants to queue: handle it elsewhere" in describe_decision(
        PlanDecision(action="answer", deferred_deep={"goal": "handle it elsewhere"}))
    assert "it also wants to queue" not in describe_decision(PlanDecision(action="answer"))


def test_describe_decision_includes_confirm_question_only_when_present():
    assert "question it wants to ask: Which one did you mean?" in describe_decision(
        PlanDecision(action="confirm", confirm_question="Which one did you mean?"))
    assert "question it wants to ask" not in describe_decision(PlanDecision(action="confirm"))


def test_describe_decision_includes_rationale_only_when_present():
    assert "its reasoning: because it is simple" in describe_decision(
        PlanDecision(action="answer", rationale="because it is simple"))
    assert "its reasoning" not in describe_decision(PlanDecision(action="answer", rationale=""))


# ---------------------------------------------------------------------------
# apply_review
# ---------------------------------------------------------------------------

def test_apply_review_an_agreeing_reviewer_returns_the_same_object():
    """Identity, since an agreeing reviewer must cost nothing and must not drop the chosen reads."""
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], rationale="because")
    reviewed = apply_review(cheap, {"action": "read", "rationale": "agreed"})
    assert reviewed is cheap


def test_apply_review_a_non_dict_response_returns_cheap_unchanged():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    assert apply_review(cheap, None) is cheap
    assert apply_review(cheap, "not a dict at all") is cheap
    assert apply_review(cheap, ["also", "not", "a", "dict"]) is cheap


def test_apply_review_a_missing_action_returns_cheap_unchanged():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    assert apply_review(cheap, {"rationale": "no action field here"}) is cheap


def test_apply_review_a_hallucinated_action_returns_cheap_unchanged():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    assert apply_review(cheap, {"action": "banana", "rationale": "nonsense"}) is cheap


def test_apply_review_deep_carries_over_model_tier_and_difficulty_fields():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], model_tier="sonnet",
                         deep_difficulty="hard", deep_difficulty_reason="a big change",
                         deep_target="code_or_files", confidence="low")
    reviewed = apply_review(cheap, {"action": "deep", "goal": "fix the bug",
                                   "deep_brief": "fix the underlying bug",
                                   "rationale": "this needs real work"})
    assert reviewed.action == "deep"
    assert reviewed.goal == "fix the bug"
    assert reviewed.deep_brief == "fix the underlying bug"
    assert reviewed.model_tier == "sonnet"
    assert reviewed.deep_difficulty == "hard"
    assert reviewed.deep_difficulty_reason == "a big change"
    assert reviewed.deep_target == "code_or_files"


def test_apply_review_answer_with_hand_off_carries_a_non_empty_deferred_goal():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    reviewed = apply_review(cheap, {"action": "answer", "hand_off": True,
                                   "rationale": "lives outside what a read can reach"})
    assert reviewed.action == "answer"
    assert reviewed.deferred_deep is not None
    assert reviewed.deferred_deep["goal"]


def test_apply_review_answer_without_hand_off_has_no_deferred_deep():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    reviewed = apply_review(cheap, {"action": "answer", "hand_off": False,
                                   "rationale": "just answer it"})
    assert reviewed.action == "answer"
    assert reviewed.deferred_deep is None


def test_apply_review_confirm_produces_a_question():
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    reviewed = apply_review(cheap, {"action": "confirm", "confirm_question": "Which one?",
                                   "rationale": "ambiguous"})
    assert reviewed.action == "confirm"
    assert reviewed.confirm_question == "Which one?"


def test_apply_review_read_with_nothing_planned_to_read_degrades_to_answer():
    """A reviewer asking for a read the cheap model never planned must not produce an empty read."""
    cheap = PlanDecision(action="answer", rationale="nothing to read")
    reviewed = apply_review(cheap, {"action": "read", "rationale": "should have read something"})
    assert reviewed.action == "answer"


def test_user_goal_fallback_is_never_empty():
    assert user_goal_fallback(PlanDecision(action="read"))
    assert user_goal_fallback(PlanDecision(action="deep", goal="do the thing")) == "do the thing"


# ---------------------------------------------------------------------------
# Fakes for Orchestrator.cascade_review
# ---------------------------------------------------------------------------

class RecordingProvider:
    """A ModelProvider whose plan() records every call and replays one scripted response."""

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


def build(provider: Any, *, web: Any = None, **cfg: Any) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(**cfg),
                        web=web)


# ---------------------------------------------------------------------------
# Orchestrator.cascade_review
# ---------------------------------------------------------------------------

def test_cascade_review_off_by_default_makes_no_call_and_returns_the_decision_unchanged():
    provider = RecordingProvider(response={"action": "answer", "rationale": "fine"})
    orch = build(provider, planner_cascade=False)
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], confidence="low")
    reviewed = orch.cascade_review(cheap, "do the thing", "some context", [])
    assert reviewed is cheap
    assert provider.calls == []
    assert orch.last_plan_cascaded is False
    assert orch.planner_cascade_reviews == 0


def test_cascade_review_makes_no_call_when_the_signal_does_not_match():
    provider = RecordingProvider(response={"action": "deep", "rationale": "overruled"})
    orch = build(provider, planner_cascade=True, planner_cascade_escalate_on="action:confirm")
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], confidence="high")
    reviewed = orch.cascade_review(cheap, "do the thing", "some context", [])
    assert reviewed is cheap
    assert provider.calls == []
    assert orch.last_plan_cascaded is False
    assert orch.planner_cascade_reviews == 0


def test_cascade_review_matching_signal_calls_once_on_the_configured_tier_and_applies_the_verdict():
    provider = RecordingProvider(response={"action": "answer", "hand_off": True,
                                           "rationale": "nothing attached can reach that"})
    orch = build(provider, planner_cascade=True, planner_cascade_tier="best")
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], confidence="low")
    reviewed = orch.cascade_review(cheap, "restart the service", "some context", [])
    assert len(provider.calls) == 1
    assert provider.calls[0]["tier"] == "best"
    assert reviewed.action == "answer"
    assert reviewed.deferred_deep is not None
    assert orch.last_plan_cascaded is True
    assert orch.planner_cascade_reviews == 1


def test_cascade_review_a_raising_provider_leaves_the_cheap_decision_standing():
    provider = RecordingProvider(raises=RuntimeError("boom"))
    orch = build(provider, planner_cascade=True)
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], confidence="low")
    reviewed = orch.cascade_review(cheap, "restart the service", "some context", [])
    assert reviewed is cheap
    assert orch.last_plan_cascaded is False
    assert orch.planner_cascade_reviews == 0


def test_cascade_review_tier_is_only_sent_to_a_provider_whose_plan_accepts_one():
    provider = RecordingProvider(response={"action": "answer", "rationale": "ok"})
    assert provider_call_accepts_tier(provider.plan) is True
    orch = build(provider, planner_cascade=True)
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}])
    orch.cascade_review(cheap, "restart the service", "some context", [])
    assert provider.calls[0]["tier"] == orch.cfg.planner_cascade_tier


def test_cascade_review_passes_web_configured_through_to_the_review_digest():
    """``self.web is not None`` must reach ``build_review_digest``, the same way it already
    reaches the reach judge and ``verdict_block``."""
    provider_no_web = RecordingProvider(response={"action": "answer", "rationale": "ok"})
    orch_no_web = build(provider_no_web, planner_cascade=True)
    cheap = PlanDecision(action="read", reads=[{"grep": "x"}], confidence="low")
    orch_no_web.cascade_review(cheap, "restart the service", "some context", [])
    assert WEB_REVIEW_NOTE not in provider_no_web.calls[0]["prompt"]

    provider_with_web = RecordingProvider(response={"action": "answer", "rationale": "ok"})
    orch_with_web = build(provider_with_web, web=object(), planner_cascade=True)
    orch_with_web.cascade_review(cheap, "restart the service", "some context", [])
    assert WEB_REVIEW_NOTE in provider_with_web.calls[0]["prompt"]
