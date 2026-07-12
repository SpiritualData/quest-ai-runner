"""WS3: intent judgment owned by a structured LLM call, regex demoted to prefilter.

``_message_requests_change`` (the regex prefilter) is decisive on its own -- and free -- for the
common cases. Only in the AMBIGUOUS band it leaves undecided (a change-verb/wrongness signal
fired, but an interrogative opener or bare "?" ending overrode it -- see
``message_change_signal_ambiguous``) does ONE structured LLM judgment
(``Orchestrator.judge_execution_directive``) step in, hard-timeout-guarded and falling back to
the regex verdict on any failure. Covers:

  * the ambiguous-band gate itself (pure function, no LLM);
  * the judgment call in isolation: success, timeout, exception, and unusable-response fallback;
  * the end-to-end wiring in ``run()``: the judge is NOT called when the regex is conclusive
    (either way) and IS called, and can change the outcome, only in the ambiguous band.

Offline, no network.
"""
import time
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    message_change_signal_ambiguous,
    _message_requests_change,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


def _orch(provider, **kw):
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


# --- the ambiguous-band gate (pure function) ----------------------------------------------------

def test_ambiguous_band_true_when_signal_present_but_regex_says_no():
    msg = "how would I fix the login bug?"
    assert _message_requests_change(msg) is False        # interrogative opener overrides
    assert message_change_signal_ambiguous(msg) is True  # but a verb + wrongness signal fired


def test_ambiguous_band_false_when_no_signal_at_all():
    msg = "thanks so much, that's really helpful!"
    assert _message_requests_change(msg) is False
    assert message_change_signal_ambiguous(msg) is False


def test_ambiguous_band_false_when_regex_already_says_yes():
    msg = "please fix the login bug in auth.py"
    assert _message_requests_change(msg) is True
    # Not part of the "ambiguous" contract -- the regex already decided -- but the signal helper
    # itself is still True here (verb present); the orchestrator's gate short-circuits on the
    # regex verdict before ever consulting ambiguity, covered by the integration test below.
    assert message_change_signal_ambiguous(msg) is True


# --- judge_execution_directive in isolation ------------------------------------------------

class _ToolRoutedProvider(StubProvider):
    """Answers the intent-directive tool call from a fixed script; everything else behaves like
    StubProvider. Lets a test target the ONE call that matters without knowing how many other
    provider.plan()/answer() calls happen around it."""

    def __init__(self, *, on_intent_call, decisions: List[Dict[str, Any]] | None = None, **kw):
        super().__init__(decisions=decisions or [], **kw)
        self._on_intent_call = on_intent_call
        self.intent_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "execution_directive_verdict":
            self.intent_calls += 1
            self.plan_calls += 1
            self.plan_prompts.append(prompt)
            return self._on_intent_call(prompt)
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def testjudge_execution_directive_returns_true_on_llm_verdict():
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: {"is_execution_directive": True, "reason": "user directed action"})
    orch = _orch(provider)
    is_directive, reason = orch.judge_execution_directive(
        "how would I fix the login bug, go ahead and do it", "I could update the auth check.")
    assert is_directive is True
    assert "user directed action" in reason


def testjudge_execution_directive_returns_false_on_llm_verdict():
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: {"is_execution_directive": False, "reason": "purely informational"})
    orch = _orch(provider)
    is_directive, reason = orch.judge_execution_directive(
        "how would I even approach fixing that bug?", "You could check the auth module.")
    assert is_directive is False
    assert "purely informational" in reason


def testjudge_execution_directive_falls_back_on_exception():
    def _raise(_prompt):
        raise RuntimeError("provider outage")
    provider = _ToolRoutedProvider(on_intent_call=_raise)
    orch = _orch(provider)
    is_directive, reason = orch.judge_execution_directive("how would I fix the bug?", "answer")
    assert is_directive is False
    assert "regex" in reason.lower()


def testjudge_execution_directive_falls_back_on_missing_key():
    provider = _ToolRoutedProvider(on_intent_call=lambda p: {"unexpected": "shape"})
    orch = _orch(provider)
    is_directive, reason = orch.judge_execution_directive("how would I fix the bug?", "answer")
    assert is_directive is False
    assert "regex" in reason.lower()


def testjudge_execution_directive_falls_back_on_timeout(monkeypatch):
    monkeypatch.setenv("QAR_INTENT_JUDGE_TIMEOUT_SECONDS", "0.05")

    def _slow(_prompt):
        time.sleep(0.5)
        return {"is_execution_directive": True, "reason": "too slow to matter"}

    provider = _ToolRoutedProvider(on_intent_call=_slow)
    orch = _orch(provider)
    started = time.monotonic()
    is_directive, reason = orch.judge_execution_directive("how would I fix the bug?", "answer")
    elapsed = time.monotonic() - started
    assert is_directive is False
    assert "regex" in reason.lower()
    assert elapsed < 0.4, "the call must not block the turn past its configured timeout"


# --- end-to-end wiring in run() -------------------------------------------------------------

def test_run_does_not_consult_llm_when_regex_is_conclusive_yes():
    # A clear command ("please fix...") is decided by the regex alone -- zero LLM judge calls.
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: (_ for _ in ()).throw(AssertionError("judge should not be called")),
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "propose"}],
        answer_text="I could update the auth check.",
    )
    runner = StubDeepRunner(met=True, output="fixed it")
    res = _orch(provider, deep_runner=runner).run("please fix the login bug in auth.py")
    assert res.kind == "answer"
    assert provider.intent_calls == 0
    assert runner.calls, "a conclusive regex match must still escalate to deep"


def test_run_does_not_consult_llm_when_no_signal_at_all():
    # A message with no change-verb/wrongness signal at all never reaches the ambiguous-band
    # check, let alone the LLM -- zero judge calls, no escalation.
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: (_ for _ in ()).throw(AssertionError("judge should not be called")),
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "chit-chat"}],
        answer_text="You're welcome!",
    )
    runner = StubDeepRunner(met=True, output="n/a")
    res = _orch(provider, deep_runner=runner).run("thanks so much, that's great!")
    assert res.kind == "answer"
    assert provider.intent_calls == 0
    assert not runner.calls


def test_run_consults_llm_in_ambiguous_band_and_escalates_when_directive():
    # Ambiguous band: verb+wrongness signal present, but the interrogative opener made the regex
    # say no. The LLM judge is consulted and, when it says "yes, this is a directive", the turn
    # escalates to deep exactly like a conclusive regex match would.
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: {"is_execution_directive": True, "reason": "go ahead and do it"},
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "propose"}],
        answer_text="I could update the login check.",
    )
    runner = StubDeepRunner(met=True, output="fixed it")
    res = _orch(provider, deep_runner=runner).run(
        "how would I fix the login bug, go ahead and do it if you can")
    assert res.kind == "answer"
    assert provider.intent_calls == 1
    assert runner.calls, "the LLM judge said yes -- the turn must still escalate to deep"


def test_run_consults_llm_in_ambiguous_band_and_stays_answer_when_not_directive():
    # Same ambiguous message, but the LLM judge says "no, not a directive" -- the turn must stay
    # a plain answer, not be force-escalated.
    provider = _ToolRoutedProvider(
        on_intent_call=lambda p: {"is_execution_directive": False, "reason": "purely exploratory"},
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "propose"}],
        answer_text="You could check the auth module for the bug.",
    )
    runner = StubDeepRunner(met=True, output="n/a")
    res = _orch(provider, deep_runner=runner).run("how would I even fix the login bug?")
    assert res.kind == "answer"
    assert provider.intent_calls == 1
    assert not runner.calls, "the LLM judge said no -- must not force-escalate"
