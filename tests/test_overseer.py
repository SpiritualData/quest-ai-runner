"""Minimal-intervention OVERSEER: digest builder, the five signals, and the wiring in run().

Offline, no network. Follows tests/test_orchestrator.py's StubProvider/StubRetrieval pattern; the
stub subclasses that split planner vs overseer calls are defined LOCALLY here (conftest.py is not
touched). The overseer resolves its own model and (via a single-provider ModelRegistry) shares the
provider with the planner, so a local StubProvider subclass routes plan() calls to a separate
overseer queue by detecting the overseer prompt.

Many tests below pass ``overseer_gate_spend_fraction=0.0`` to make hook A's Fix-12 cheap pre-filter
gate PERMISSIVE (it always says "worth a look" once ANY time has elapsed, which is always true by
the time hook A's submit site runs), so tests about redirect/answer_now/escalate mechanics are not
entangled with the gate's own behavior. The gate itself is tested separately, with DEFAULT settings,
further down.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import (
    EVENT_OVERSEER,
    EVENT_PLAN,
    Mode,
    MilestoneSink,
    ProgressEvent,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    _oversee_worth_a_look,
    _prior_escalation_lines,
    _recent_conversation_turns,
)
from quest_ai_runner.core.overseer import (
    OVERSEER_PROMPT,
    OverseerSignal,
    build_digest,
    oversee,
)

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


# The marker that identifies an overseer plan() call (the prompt is the OVERSEER_PROMPT).
_OVERSEER_MARK = "OVERSEER"

# A permissive gate config: always lets hook A submit (see module docstring).
_PERMISSIVE_GATE = dict(overseer_gate_spend_fraction=0.0)


class OverseerStubProvider(StubProvider):
    """StubProvider that routes plan() calls: overseer prompts draw from ``overseer_signals``,
    everything else from the normal ``decisions`` queue. Tracks how many overseer calls happened."""

    def __init__(self, decisions: List[Dict[str, Any]], *,
                 overseer_signals: Optional[List[Dict[str, Any]]] = None,
                 answer_text: str = "STUB ANSWER"):
        super().__init__(decisions, answer_text=answer_text)
        self._overseer_signals = list(overseer_signals or [])
        self.overseer_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if _OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            self.overseer_calls += 1
            if self._overseer_signals:
                return self._overseer_signals.pop(0)
            return {"signal": "proceed"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


class RaisingOverseerProvider(StubProvider):
    """Like OverseerStubProvider but the overseer plan() call RAISES, to prove it degrades to
    proceed and the run still completes."""

    def __init__(self, decisions: List[Dict[str, Any]], answer_text: str = "STUB ANSWER"):
        super().__init__(decisions, answer_text=answer_text)
        self.overseer_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if _OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            self.overseer_calls += 1
            raise RuntimeError("overseer provider blew up")
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


def _events(sink_events: List[ProgressEvent]) -> List[str]:
    return [e.type for e in sink_events]


class _CaptureSink:
    def __init__(self):
        self.events: List[ProgressEvent] = []

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# (a) overseer=False -> zero overseer calls and identical event stream to baseline.
# ---------------------------------------------------------------------------

def test_overseer_off_is_zero_calls_and_identical_stream():
    decisions = [{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}]

    base_provider = OverseerStubProvider(list(decisions))
    base_sink = _CaptureSink()
    _orch(base_provider, StubRetrieval(),
          config=OrchestratorConfig(overseer=False, answer_goal_max_iterations=1)).run(
        "explain X", sink=base_sink)

    off_provider = OverseerStubProvider(list(decisions), overseer_signals=[{"signal": "proceed"}])
    off_sink = _CaptureSink()
    _orch(off_provider, StubRetrieval(),
          config=OrchestratorConfig(overseer=False, answer_goal_max_iterations=1)).run(
        "explain X", sink=off_sink)

    assert off_provider.overseer_calls == 0
    assert base_provider.overseer_calls == 0
    # Identical event streams (no EVENT_OVERSEER anywhere).
    assert _events(off_sink.events) == _events(base_sink.events)
    assert EVENT_OVERSEER not in _events(off_sink.events)


# ---------------------------------------------------------------------------
# (b) proceed is a no-op but still emits EVENT_OVERSEER with signal proceed.
# ---------------------------------------------------------------------------

def test_proceed_is_noop_but_emits_overseer_event():
    provider = OverseerStubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
        overseer_signals=[{"signal": "proceed", "reason": "on track"}],
    )
    sink = _CaptureSink()
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(overseer=True, answer_goal_max_iterations=1)).run(
        "explain X", sink=sink)
    assert res.kind == "answer"
    # An answer on step 1 leaves the loop immediately, so hook A does NOT submit (it only fires on
    # a "read" plan that will have a next step to apply a signal to). The answer checkpoint (hook B)
    # still consults once (not gated), proceeds, and emits its EVENT_OVERSEER.
    assert provider.overseer_calls >= 1
    ov = [e for e in sink.events if e.type == EVENT_OVERSEER]
    assert len(ov) == len(res.overseer_signals)
    assert all(e.data.get("signal") == "proceed" for e in ov)
    assert res.overseer_signals and all(s["signal"] == "proceed" for s in res.overseer_signals)


# ---------------------------------------------------------------------------
# (c) redirect injects "COURSE CORRECTION: <hint>" into the NEXT planner prompt.
# ---------------------------------------------------------------------------

def test_redirect_injects_course_correction_into_next_planner_prompt():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read step 1"},
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer step 2"},
        ],
        overseer_signals=[{"signal": "redirect", "hint": "focus on the config file, not the readme"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    # overseer_poll_timeout_seconds>0 makes the background hook-A consult resolve deterministically
    # at the next poll, so the redirect is reliably applied before step 2's planner runs.
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, overseer_poll_timeout_seconds=5.0,
                                          answer_goal_max_iterations=1, **_PERMISSIVE_GATE)).run(
        "explain X", sink=_CaptureSink())
    assert res.kind == "answer"
    # The second planner prompt (step 2) must carry the course correction observation.
    planner_prompts = [p for p in provider.plan_prompts if _OVERSEER_MARK not in p]
    assert len(planner_prompts) >= 2
    assert "COURSE CORRECTION: focus on the config file, not the readme" in planner_prompts[1]


# ---------------------------------------------------------------------------
# (d) answer_now short-circuits a read-looping planner -> exit_reason overseer_answer_now.
# ---------------------------------------------------------------------------

def test_answer_now_short_circuits_read_loop():
    # Planner would keep reading forever; the overseer says answer_now on step 1.
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"}
            for _ in range(10)
        ],
        overseer_signals=[{"signal": "answer_now", "reason": "enough gathered"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    # overseer_poll_timeout_seconds>0 makes the (now background) hook-A consult resolve
    # deterministically at the next poll instead of relying on thread timing.
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, max_steps=10,
                                          overseer_poll_timeout_seconds=5.0,
                                          answer_goal_max_iterations=1, **_PERMISSIVE_GATE)).run(
        "q", sink=_CaptureSink())
    assert res.kind == "answer"
    assert res.exit_reason == "overseer_answer_now"
    # Non-blocking overseer (Fix 1): the answer_now submitted while planning step 1 is applied ONE
    # STEP LATE, at the top of step 2, which is the documented tradeoff for never stalling the loop.
    assert res.steps == 2
    # Hook A submitted at step 1 and its answer_now is applied at step 2's poll.
    assert provider.overseer_calls >= 1
    assert res.overseer_signals[0]["signal"] == "answer_now"


# ---------------------------------------------------------------------------
# (e) escalate_deep forces deep with a stub deep runner -> exit_reason overseer_escalated_deep.
# ---------------------------------------------------------------------------

def test_escalate_deep_forces_deep():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"}
            for _ in range(10)
        ],
        overseer_signals=[{"signal": "escalate_deep", "reason": "needs real execution"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    runner = StubDeepRunner(met=True, output="did the deep work")
    res = _orch(provider, retrieval, deep_runner=runner,
                config=OrchestratorConfig(overseer=True, max_steps=10,
                                          overseer_poll_timeout_seconds=5.0, **_PERMISSIVE_GATE)).run(
                    "do it", sink=_CaptureSink())
    assert res.kind == "deep"
    assert res.exit_reason == "overseer_escalated_deep"
    assert len(res.deep_results) == 1 and res.deep_results[0].met is True


# ---------------------------------------------------------------------------
# (e2) Fix 2: escalate_human routes through confirm/decision-request, NOT deep execution.
# ---------------------------------------------------------------------------

def test_escalate_human_routes_to_confirm_not_deep():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"}
            for _ in range(10)
        ],
        overseer_signals=[{"signal": "escalate_human",
                           "reason": "this needs your explicit authorization"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    runner = StubDeepRunner(met=True, output="should NOT be reached")
    escalation = StubEscalation(decision_id="dec_999")
    res = _orch(provider, retrieval, deep_runner=runner, escalation=escalation,
                config=OrchestratorConfig(overseer=True, max_steps=10,
                                          overseer_poll_timeout_seconds=5.0, **_PERMISSIVE_GATE)).run(
                    "delete the production database", sink=_CaptureSink())
    assert res.kind == "confirm"
    assert res.exit_reason == "overseer_escalated_human"
    assert res.decision_id == "dec_999"
    # A REAL decision-request was raised via the escalation sink (durable, human-facing).
    assert len(escalation.raised) == 1
    # Deep execution must NOT have run for a human-only fork.
    assert runner.calls == []


# ---------------------------------------------------------------------------
# (f) an overseer call that raises degrades to proceed and the run completes.
# ---------------------------------------------------------------------------

def test_overseer_raise_degrades_to_proceed():
    provider = RaisingOverseerProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
    )
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(overseer=True, answer_goal_max_iterations=1)).run(
        "explain X", sink=_CaptureSink())
    assert res.kind == "answer"  # run still completed
    assert provider.overseer_calls >= 1  # the overseer was consulted (and blew up)


# ---------------------------------------------------------------------------
# (g) overseer_max_signals=1 -> exactly one overseer call across a multi-step run.
# ---------------------------------------------------------------------------

def test_max_signals_caps_consultations():
    provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"},
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"},
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
        ],
        overseer_signals=[{"signal": "proceed"}, {"signal": "proceed"}, {"signal": "proceed"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    res = _orch(provider, retrieval,
                config=OrchestratorConfig(overseer=True, overseer_max_signals=1, max_steps=5,
                                          overseer_poll_timeout_seconds=5.0,
                                          answer_goal_max_iterations=1, **_PERMISSIVE_GATE)).run(
                    "q", sink=_CaptureSink())
    assert res.kind == "answer"
    assert provider.overseer_calls == 1
    assert res.overseer_signals is not None and len(res.overseer_signals) == 1


# ---------------------------------------------------------------------------
# (h) build_digest respects the char budget, includes pass + token counts, excludes full bodies.
# ---------------------------------------------------------------------------

def test_build_digest_budget_and_contents():
    big_body = "SECRET_FULL_BODY " * 500  # a full observation body that must NOT appear verbatim
    digest = build_digest(
        user_message="What does the config do?",
        step=2,
        max_steps=6,
        plan_action="read",
        plan_rationale="looking at config",
        plan_goal="Understand config",
        tokens_in=1234,
        tokens_out=567,
        elapsed_seconds=3.0,
        max_elapsed_seconds=60.0,
        gathered_chars=9000,
        max_gathered_chars=40000,
        consecutive_reads=2,
        char_budget=400,
    )
    assert len(digest) <= 400
    assert "PASS: 2 of 6" in digest
    assert "1234" in digest and "567" in digest  # token counts present
    assert big_body.strip() not in digest  # full observation body excluded


def test_oversee_unknown_or_error_returns_proceed():
    class _Bad:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "nonsense"}
    assert oversee(_Bad(), "m", "digest").signal == "proceed"

    class _Boom:
        def plan(self, prompt, *, model, tool_schema):
            raise RuntimeError("boom")
    assert oversee(_Boom(), "m", "digest").signal == "proceed"

    class _Redirect:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "redirect", "hint": "do X instead", "reason": "off subject"}
    sig = oversee(_Redirect(), "m", "digest")
    assert sig.signal == "redirect" and sig.hint == "do X instead"

    # A non-redirect signal must never carry a hint.
    class _AnswerNowWithHint:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "answer_now", "hint": "should be dropped"}
    sig2 = oversee(_AnswerNowWithHint(), "m", "digest")
    assert sig2.signal == "answer_now" and sig2.hint == ""

    # Fix 2: both new signals are recognized and round-trip cleanly.
    class _EscalateDeep:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "escalate_deep", "reason": "needs execution"}
    assert oversee(_EscalateDeep(), "m", "digest").signal == "escalate_deep"

    class _EscalateHuman:
        def plan(self, prompt, *, model, tool_schema):
            return {"signal": "escalate_human", "reason": "needs a human"}
    assert oversee(_EscalateHuman(), "m", "digest").signal == "escalate_human"


# ---------------------------------------------------------------------------
# Fix 1 (item 1): the em-dash INSTRUCTION was removed from OVERSEER_PROMPT; the AUTHORED prompt
# text itself must still never contain a literal em dash (repo-wide copy convention).
# ---------------------------------------------------------------------------

def test_overseer_prompt_authored_text_has_no_em_dash():
    assert "—" not in OVERSEER_PROMPT


def test_overseer_prompt_flags_action_requests_and_promising_drafts():
    """Qualitative testing showed the overseer let action requests (make a change / run / commit)
    slide by as 'proceed', and let a draft that only PROMISES the work pass at the answer
    checkpoint. The prompt must keep the guidance that fixes both, so those escalate cases don't
    silently regress if the prompt is later edited.
    """
    low = OVERSEER_PROMPT.lower()
    # An action REQUEST met by a read-and-answer plan is an escalate_deep, not a proceed.
    assert "action verb" in low
    assert "escalate_deep" in low
    # A draft that only recommends/promises the work has not done it: escalate_deep.
    assert "draft answer" in low
    assert "promises" in low or "promise" in low


def test_overseer_prompt_biases_escalate_human_toward_genuine_forks_only():
    """Fix 2: escalate_human must be reserved for genuine human-only forks (identity, irreversible
    actions, authorization, true ambiguity), mirroring the org's "AI acts first" principle, so it
    does not over-trigger on routine automatable work."""
    low = OVERSEER_PROMPT.lower()
    assert "escalate_human" in low
    assert "identity" in low
    assert "irreversible" in low
    # The bias-toward-AI-action language must be present so escalate_human stays rare.
    assert "ai acts first" in low or "genuine" in low


# ---------------------------------------------------------------------------
# (i) EVENT_OVERSEER passes through a MilestoneSink (BACKGROUND); EVENT_PLAN does not.
# ---------------------------------------------------------------------------

def test_overseer_event_surfaces_in_milestone_sink_but_plan_does_not():
    seen_overseer: List[ProgressEvent] = []
    sink = MilestoneSink(on_overseer=lambda e: seen_overseer.append(e))
    # EVENT_PLAN is chatter — dropped by a MilestoneSink.
    sink.update(ProgressEvent(type=EVENT_PLAN, action="read", step=1), Mode.BACKGROUND)
    assert seen_overseer == []
    # EVENT_OVERSEER always surfaces — forwarded to on_overseer.
    sink.update(ProgressEvent(type=EVENT_OVERSEER, step=1,
                              data={"signal": "redirect", "hint": "h"}), Mode.BACKGROUND)
    assert len(seen_overseer) == 1
    assert seen_overseer[0].data.get("signal") == "redirect"


# ---------------------------------------------------------------------------
# (j) Fix 2 (relabel) + Fix 3->QUALITY BAR: the read-budget line is unambiguous, and the QUALITY
#     BAR line appears only when quality_standards is provided.
# ---------------------------------------------------------------------------

def test_digest_quality_bar_and_relabeled_read_budget():
    d_with = build_digest(
        user_message="do the thing", step=1, max_steps=3,
        quality_standards="Must pass all tests and update the docs",
        gathered_chars=5200, max_gathered_chars=40000,
    )
    assert "QUALITY BAR: Must pass all tests and update the docs" in d_with
    # the read-budget line names whose budget it is and disclaims the digest-size misread.
    assert "AGENT'S READ BUDGET:" in d_with
    assert "NOT this digest's size" in d_with
    assert "READING: 5200 of 40000 chars used" not in d_with  # the old ambiguous label is gone

    # QUALITY BAR is omitted entirely when there is nothing to say (keeps the digest tiny).
    d_without = build_digest(user_message="do the thing", step=1, max_steps=3)
    assert "QUALITY BAR" not in d_without


# ---------------------------------------------------------------------------
# Fix 4 (corrected): CURRENT USER REQUEST always shows the RAW user_message verbatim; RESOLVED AS
# is an ADDITIONAL line only when goal_condition differs, never a silent replacement.
# ---------------------------------------------------------------------------

def test_digest_current_user_request_and_resolved_as():
    d_same = build_digest(user_message="do it", goal_condition="do it", step=1, max_steps=3)
    assert "CURRENT USER REQUEST: do it" in d_same
    assert "RESOLVED AS" not in d_same

    d_diff = build_digest(
        user_message="do it",
        goal_condition="add a --dry-run flag to poll and commit it",
        step=1, max_steps=3,
    )
    assert "CURRENT USER REQUEST: do it" in d_diff
    assert "RESOLVED AS: add a --dry-run flag to poll and commit it" in d_diff

    # No goal_condition at all -> just the raw request, no RESOLVED AS line.
    d_none = build_digest(user_message="do it", step=1, max_steps=3)
    assert "CURRENT USER REQUEST: do it" in d_none
    assert "RESOLVED AS" not in d_none


# ---------------------------------------------------------------------------
# Fix 5a: RECENT CONVERSATION section + the dedup-against-current-request helper.
# ---------------------------------------------------------------------------

def test_digest_recent_conversation_section():
    d = build_digest(
        user_message="q", step=1, max_steps=3,
        recent_conversation=["What does config.py do?", "Where is the planner?"],
    )
    assert "RECENT CONVERSATION (last 2 turns):" in d
    assert "What does config.py do?" in d
    assert "Where is the planner?" in d

    # Omitted entirely when empty (keeps the digest tiny when there's no history).
    d_empty = build_digest(user_message="q", step=1, max_steps=3)
    assert "RECENT CONVERSATION" not in d_empty


def test_recent_conversation_turns_excludes_current_request_and_other_conversations():
    conv_ctx_text = (
        "=== CURRENT CONVERSATION ===\n"
        "USER: What does config.py do?\n"
        "ASSISTANT: It defines RunnerConfig.\n"
        "USER: add a --dry-run flag to poll and commit it\n"
        "\n=== OTHER PAST CONVERSATIONS (may be unrelated) ===\n"
        "USER: unrelated old thing from a different thread\n"
    )
    # The CURRENT turn's own request (raw, verbatim) must be excluded so it never duplicates
    # against CURRENT USER REQUEST, and the OTHER PAST CONVERSATIONS block must be ignored entirely
    # (a genuinely different conversation, out of scope for "this same conversation").
    turns = _recent_conversation_turns(
        conv_ctx_text, exclude=["add a --dry-run flag to poll and commit it"], max_turns=3)
    assert turns == ["What does config.py do?"]
    assert "unrelated old thing" not in turns
    assert "add a --dry-run flag to poll and commit it" not in turns


def test_recent_conversation_turns_caps_to_max_turns():
    conv_ctx_text = "=== CURRENT CONVERSATION ===\n" + "".join(
        f"USER: turn number {i}\n" for i in range(5)
    )
    turns = _recent_conversation_turns(conv_ctx_text, exclude=[], max_turns=2)
    assert turns == ["turn number 3", "turn number 4"]


def test_recent_conversation_turns_empty_or_missing_marker_returns_empty():
    assert _recent_conversation_turns("", exclude=[]) == []
    assert _recent_conversation_turns("no marker here", exclude=[]) == []


# ---------------------------------------------------------------------------
# Fix 7: PRIOR ESCALATIONS THIS CONVERSATION section + its formatting helper.
# ---------------------------------------------------------------------------

def test_prior_escalation_lines_formats_and_numbers():
    lines = _prior_escalation_lines([
        {"kind": "deep", "outcome": "deep_met"},
        {"kind": "human", "exit_reason": "confirm"},
    ])
    assert lines == [
        "1: escalated to deep, outcome: deep_met",
        "2: escalated to human, outcome: confirm",
    ]
    assert _prior_escalation_lines(None) == []
    assert _prior_escalation_lines([]) == []


def test_digest_prior_escalations_section_default_and_populated():
    d_empty = build_digest(user_message="q", step=1, max_steps=3)
    # Fix 7: unlike other optional sections, this one ALWAYS appears (even "none yet").
    assert "PRIOR ESCALATIONS THIS CONVERSATION: none yet" in d_empty

    d_full = build_digest(
        user_message="q", step=1, max_steps=3,
        prior_escalations=["1: escalated to deep, outcome: deep_met"],
    )
    assert "PRIOR ESCALATIONS THIS CONVERSATION:" in d_full
    assert "1: escalated to deep, outcome: deep_met" in d_full
    assert "PRIOR ESCALATIONS THIS CONVERSATION: none yet" not in d_full


# ---------------------------------------------------------------------------
# Fix 5b: OPERATIONS THIS TURN — numbered, kind-tagged, reflects the TRUE total.
# ---------------------------------------------------------------------------

def test_digest_operations_this_turn_numbered_and_tagged():
    d = build_digest(
        user_message="q", step=1, max_steps=3,
        operations=["[read] cli.py [head]: found argparse subcommands"],
        operations_total=5,
    )
    assert "OPERATIONS THIS TURN (5 so far):" in d
    # Numbered using the TRUE total (5), not the length of the shown window (1): the single shown
    # item is the 5th operation overall.
    assert "5. [read] cli.py [head]: found argparse subcommands" in d

    d_empty = build_digest(user_message="q", step=1, max_steps=3)
    assert "OPERATIONS THIS TURN: none yet" in d_empty


# ---------------------------------------------------------------------------
# Fix 8: truncation order — PASS/CURRENT PLAN/RATIONALE/SPEND/TIME/AGENT'S READ BUDGET always
# survive even when the sheddable "history" sections (RECENT CONVERSATION, OPERATIONS THIS TURN)
# are huge and the overall char_budget is tight.
# ---------------------------------------------------------------------------

def test_truncation_protects_essential_fields_over_history_sections():
    huge_conversation = [f"turn {i} " + ("x" * 150) for i in range(20)]
    huge_operations = [f"[read] file{i}.py " + ("y" * 150) for i in range(20)]
    digest = build_digest(
        user_message="fix the bug",
        step=3, max_steps=6,
        plan_action="read", plan_goal="find the bug", plan_rationale="checking logs",
        recent_conversation=huge_conversation,
        operations=huge_operations,
        operations_total=40,
        tokens_in=100, tokens_out=50,
        elapsed_seconds=5.0, max_elapsed_seconds=60.0,
        gathered_chars=200, max_gathered_chars=1000,
        consecutive_reads=1,
        char_budget=450,
    )
    assert len(digest) <= 450
    # These MUST survive in full, regardless of how huge the history sections were.
    assert "PASS: 3 of 6" in digest
    assert "CURRENT PLAN: action=read, goal=find the bug" in digest
    assert "RATIONALE: checking logs" in digest
    assert "SPEND: tokens_in=100 tokens_out=50; consecutive_reads=1" in digest
    assert "TIME: 5s of 60s budget" in digest
    assert "AGENT'S READ BUDGET: 200 of 1000 chars gathered" in digest
    # The user's own request is also always first/protected.
    assert "CURRENT USER REQUEST: fix the bug" in digest
    # The huge sheddable content could not possibly all fit; at least SOME of it was cut.
    full_untruncated_len = len("\n".join(huge_conversation) + "\n".join(huge_operations))
    assert full_untruncated_len > 450  # sanity check the test data is actually "huge"


def test_truncation_last_resort_when_essential_alone_exceeds_budget():
    # A pathologically tiny char_budget: even the last-resort tail-truncation must return SOMETHING
    # bounded, never raise, never exceed budget.
    digest = build_digest(
        user_message="a fairly long user request that alone exceeds a tiny budget",
        step=1, max_steps=3, char_budget=20,
    )
    assert len(digest) <= 20


# ---------------------------------------------------------------------------
# Fix 1: the overseer consult is NON-BLOCKING. Submitting returns immediately even while the
# provider call is still running, and a late-resolving signal is applied on a later poll.
# ---------------------------------------------------------------------------

class _BlockingOverseerProvider(StubProvider):
    """Overseer plan() blocks on ``release`` so a test can prove the SUBMIT caller returns before
    the provider call completes, and that a later poll picks the signal up once it resolves."""

    def __init__(self, signal: str = "answer_now", reason: str = "resolved after blocking"):
        super().__init__(decisions=[])
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self._signal = signal
        self._reason = reason

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if _OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            self.started.set()
            self.release.wait(timeout=5)
            self.finished.set()
            return {"signal": self._signal, "reason": self._reason}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def test_submit_oversee_is_non_blocking_and_late_signal_applies_on_next_poll():
    provider = _BlockingOverseerProvider()
    orch = _orch(provider, StubRetrieval())
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        signals: List[Dict[str, Any]] = []
        t0 = time.monotonic()
        # gate=False: this test is about the submit/collect plumbing, not the Fix-12 gate.
        pending = orch._submit_oversee(executor, user_message="q", step=1, plan=None,
                                       gathered=[], started=t0, gate=False)
        # 1) NON-BLOCKING: submit returned promptly though the provider call is still blocked.
        assert pending is not None
        assert time.monotonic() - t0 < 1.0
        assert provider.started.wait(timeout=2)   # the background call did start
        assert not provider.finished.is_set()     # ...and has NOT finished yet

        # 2) A non-blocking poll (timeout=0.0) while it is still running returns None and records
        #    nothing (the caller proceeds as if "proceed").
        assert orch._collect_oversee(pending, signals=signals, emit=None, timeout=0.0) is None
        assert signals == []

        # 3) Let it resolve; a later poll applies the (late) signal and records exactly one entry.
        provider.release.set()
        sig = orch._collect_oversee(pending, signals=signals, emit=None, timeout=5.0)
        assert sig is not None and sig.signal == "answer_now"
        assert len(signals) == 1 and signals[0]["signal"] == "answer_now"
    finally:
        provider.release.set()
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Fix 11: hook B is ALSO non-blocking. A slow overseer consult must NOT make the answer wait; the
# run ships the draft promptly, and a late escalate_human is finished in the background (raising a
# real decision-request via the escalation sink).
# ---------------------------------------------------------------------------

def test_hook_b_ships_answer_without_waiting_for_slow_overseer():
    provider = _BlockingOverseerProvider(signal="proceed")
    escalation = StubEscalation()
    orch = _orch(
        provider, StubRetrieval(), escalation=escalation,
        config=OrchestratorConfig(
            overseer=True, answer_goal_max_iterations=1,
            # Defaults: overseer_poll_timeout_seconds=0.0 (non-blocking quick check for hook B too).
            overseer_background_finish_timeout_seconds=5.0,
        ),
    )
    t0 = time.monotonic()
    res = orch.run("explain X", sink=_CaptureSink())
    elapsed = time.monotonic() - t0
    # The run must complete promptly (well under the 5s the overseer provider is blocked for),
    # proving hook B did NOT wait for the slow consult.
    assert elapsed < 2.0
    assert res.kind == "answer"
    provider.release.set()  # let the background thread's provider call finish; avoid leaking


def test_hook_b_background_finish_raises_decision_for_late_escalate_human():
    provider = _BlockingOverseerProvider(signal="escalate_human", reason="needs your OK")
    escalation = StubEscalation(decision_id="dec_late")
    orch = _orch(
        provider, StubRetrieval(), escalation=escalation,
        config=OrchestratorConfig(
            overseer=True, answer_goal_max_iterations=1,
            overseer_background_finish_timeout_seconds=5.0,
        ),
    )
    res = orch.run("explain X", sink=_CaptureSink())
    assert res.kind == "answer"  # the answer shipped without waiting
    assert escalation.raised == []  # nothing raised YET (the consult hadn't resolved)

    # Now let the slow consult resolve; the background finisher should pick it up and raise a real
    # decision-request within a short window.
    provider.release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not escalation.raised:
        time.sleep(0.02)
    assert len(escalation.raised) == 1


# ---------------------------------------------------------------------------
# Fix 12: cheap, non-LLM pre-filter gate for hook A.
# ---------------------------------------------------------------------------

def test_oversee_worth_a_look_pure_function():
    base = dict(consecutive_reads=0, plan_repeats_prev=False, elapsed_seconds=0.0,
               max_elapsed_seconds=60.0, gathered_chars=0, max_gathered_chars=1000,
               min_consecutive_reads=2, gate_repeat_plan=True, spend_fraction=0.6)

    # Nothing risky yet -> not worth a look.
    assert _oversee_worth_a_look(**base) is False

    # Consecutive-reads threshold crossed.
    assert _oversee_worth_a_look(**{**base, "consecutive_reads": 2}) is True

    # Plan repeats the previous step's (looping signal), gate enabled.
    assert _oversee_worth_a_look(**{**base, "plan_repeats_prev": True}) is True
    # ...but not when the repeat-plan gate itself is disabled.
    assert _oversee_worth_a_look(**{**base, "plan_repeats_prev": True,
                                    "gate_repeat_plan": False}) is False

    # Elapsed time crossed the spend fraction.
    assert _oversee_worth_a_look(**{**base, "elapsed_seconds": 40.0}) is True

    # Gathered-read volume crossed the spend fraction.
    assert _oversee_worth_a_look(**{**base, "gathered_chars": 700}) is True

    # Never raises on a bad/zero max value.
    assert _oversee_worth_a_look(**{**base, "max_elapsed_seconds": 0.0,
                                    "max_gathered_chars": 0}) is False


def test_hook_a_gate_skips_a_clean_run_but_fires_on_a_looping_run():
    # CLEAN run: one read, then answer. With DEFAULT gate thresholds, hook A never sees enough
    # signal to submit (only 0 or 1 consecutive reads ever precede its submit check), so the ONLY
    # overseer call this run makes is hook B's one-time answer checkpoint.
    clean_provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "read"},
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
        ],
        overseer_signals=[{"signal": "proceed"}],
    )
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    clean_res = _orch(clean_provider, retrieval,
                      config=OrchestratorConfig(overseer=True, max_steps=5,
                                                answer_goal_max_iterations=1)).run(
                          "q", sink=_CaptureSink())
    assert clean_res.kind == "answer"
    assert clean_provider.overseer_calls == 1  # hook B only; hook A's gate skipped it

    # LOOPING run: the planner repeats the EXACT SAME read plan (same action+goal) step after
    # step -- a clear sign of looping. The repeat-plan gate signal fires as soon as there IS a
    # previous step to compare against, so hook A submits in addition to hook B.
    looping_provider = OverseerStubProvider(
        decisions=[
            {"action": "read", "reads": [{"rel_path": "README.md"}],
             "goal": "find the answer", "rationale": "read"}
            for _ in range(4)
        ] + [{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
        overseer_signals=[{"signal": "proceed"}, {"signal": "proceed"}, {"signal": "proceed"}],
    )
    looping_res = _orch(looping_provider, retrieval,
                        config=OrchestratorConfig(overseer=True, max_steps=6,
                                                  overseer_poll_timeout_seconds=5.0,
                                                  answer_goal_max_iterations=1)).run(
                            "q", sink=_CaptureSink())
    assert looping_res.kind == "answer"
    assert looping_provider.overseer_calls > clean_provider.overseer_calls
    assert looping_provider.overseer_calls >= 2  # at least one hook-A submit + hook B
