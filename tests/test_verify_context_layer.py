"""Conscious overseer sees full context (HANDS_FREE_QUEST_AI_DESIGN.md sections 4 and 6, the WS4
follow-up gap): ``_verify_goal``'s L2 context layer.

Before this, ``_verify_goal`` judged a run's output with an EMPTY L2: the verifier never saw the
turn's assembled context (cards / grounding), only the goal, brief, transcript, and output. This
threads the SAME rendered L2 context block the turn's other calls carry into BOTH verify call sites
(the deep-goal loop and the answer-verification loop), passed straight through -- never re-rendered
-- so the marginal cost to an already-cached lineage is a cache read, not a fresh write.

Covers:
(a) direct unit tests on ``_verify_goal``'s new ``context_layer`` parameter: it lands unmodified in
    both the layered ``context`` block and the flattened fallback prompt (in a clearly labeled
    section before the worker output); an absent context_layer is byte-for-byte the old prompt/layers
    shape (no regression for a deployment that never sets it).
(b) the truncation cap (``QAR_VERIFY_CONTEXT_MAX_CHARS`` / ``verify_context_max_chars`` /
    ``truncate_verify_context``): drops only the TAIL, keeps the HEAD, notes the cut, and a
    non-positive cap disables truncation.
(c) integration through the real call sites: the deep-goal loop's verify call carries the SAME L2
    bytes as the context the deep worker itself received; the answer-verification loop's verify call
    carries the SAME L2 bytes as the answer call it is judging (and stays stable across regenerate
    iterations).
(d) the unverified-verdict contract is unchanged: verdict None on every-tier failure, regardless of
    whether a context_layer was supplied.

All offline: no network, no API key.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext, DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    grounding_context_layer,
    truncate_verify_context,
    verify_context_max_chars,
)

from .conftest import StubProvider, StubRetrieval


# --------------------------------------------------------------------------- #
# Stubs: a provider whose plan()/answer() accept and RECORD the ``layers`` kwarg
# --------------------------------------------------------------------------- #

class LayeredScriptedProvider:
    """plan() replays PLANNER decisions and VERIFIER verdicts (told apart by tool schema name);
    answer() replays scripted replies. Both accept and record ``layers`` so a test can inspect
    exactly what each call's L1/L2/L3 shape was."""

    def __init__(self, plans: Optional[List[Dict[str, Any]]] = None,
                 verdicts: Optional[List[Dict[str, Any]]] = None,
                 answer_replies: Optional[List[str]] = None):
        self._plans = list(plans or [])
        self._verdicts = list(verdicts or [])
        self._answer_replies = list(answer_replies or [])
        self.plan_layers_calls: List[Any] = []
        self.verify_layers_calls: List[Any] = []
        self.verify_prompts: List[str] = []
        self.answer_layers_calls: List[Any] = []
        self.answer_calls = 0
        self.plan_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             layers: Any = None) -> Dict[str, Any]:
        self.plan_calls += 1
        if tool_schema.get("name") == "goal_verdict":
            self.verify_layers_calls.append(layers)
            self.verify_prompts.append(prompt)
            if self._verdicts:
                return self._verdicts.pop(0)
            return {"met": True, "reason": "fallback"}
        self.plan_layers_calls.append(layers)
        if self._plans:
            return self._plans.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None, layers: Any = None) -> str:
        self.answer_calls += 1
        self.answer_layers_calls.append(layers)
        if self._answer_replies:
            return self._answer_replies.pop(0)
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class RecordingRunner:
    """A DeepRunner recording every run_goal call, including its context_preamble -- the block the
    worker ACTUALLY received, which is what verify's context_layer must be traceable to."""

    def __init__(self, results: Optional[List[DeepResult]] = None):
        self._results = list(results or [])
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model,
                           "context_preamble": context_preamble})
        if self._results:
            return self._results.pop(0)
        return DeepResult(met=True, output="done")


class PerGoalAssembler:
    """A ContextAssembler returning a context_view keyed by the task text (see
    tests/test_per_goal_context_iteration.py for the original of this stub)."""

    def __init__(self, mapping: Dict[str, str]):
        self.mapping = mapping

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        for key, view in self.mapping.items():
            if key in (task_text or ""):
                return AssembledContext(context_view=view)
        return AssembledContext(context_view="")

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


def cache_blocks_text(layers: Any) -> List[str]:
    """The text of every cache-eligible (L1/L2) block in a captured ``layers`` list, in order."""
    return [b["text"] for b in (layers or []) if b.get("cache")]


def make_orch(provider, retrieval=None, **kw) -> Orchestrator:
    return Orchestrator(retrieval=retrieval if retrieval is not None else StubRetrieval({}),
                        provider=provider, registry=ModelRegistry(provider), **kw)


# --------------------------------------------------------------------------- #
# (a) Direct unit tests: context_layer lands unmodified, absence is a no-op
# --------------------------------------------------------------------------- #

def test_verify_goal_with_no_context_layer_is_unchanged():
    # No context_layer given (the old call shape): the layered call carries no context block (only
    # the tail), and the flattened prompt has no CONTEXT section -- byte-for-byte the pre-feature
    # shape, so a deployment that never wires context is unaffected.
    provider = LayeredScriptedProvider(verdicts=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the worker output")
    assert verdict is not None and verdict["met"] is True
    assert error is None
    # No persona/standards/context given -> no cache-eligible block at all, only the volatile tail.
    assert cache_blocks_text(provider.verify_layers_calls[0]) == []
    assert "CONTEXT AVAILABLE TO THE WORKER" not in provider.verify_prompts[0]


def test_verify_goal_context_layer_lands_unmodified_in_the_layered_context_block():
    provider = LayeredScriptedProvider(verdicts=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    ctx = "CARD alpha\n  - (note) a specific fact the worker read"
    verdict, error = orch._verify_goal("the goal", "the brief", "the worker output",
                                       context_layer=ctx)
    assert verdict is not None and error is None
    cache_texts = cache_blocks_text(provider.verify_layers_calls[0])
    # Head is empty (no persona/standards given), so the only cache=True block is the context layer,
    # and it is EXACTLY the string passed in -- never re-rendered.
    assert cache_texts == [ctx]


def test_verify_goal_context_layer_appears_in_the_flattened_fallback_before_output():
    # A provider without the layered surface (StubProvider.plan has no ``layers`` param) must still
    # see the context, inline, in a clearly labeled section BEFORE the worker output it grounds.
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    ctx = "UNIQUE_CONTEXT_MARKER_998"
    verdict, error = orch._verify_goal("the goal", "the brief", "THE WORKER OUTPUT TEXT",
                                       context_layer=ctx)
    assert verdict is not None and error is None
    prompt = provider.last_plan_prompt
    assert "CONTEXT AVAILABLE TO THE WORKER" in prompt
    assert ctx in prompt
    ctx_idx = prompt.index(ctx)
    output_idx = prompt.index("--- WORKER OUTPUT")
    assert ctx_idx < output_idx, "context must be placed before the output-to-judge section"


def test_verify_goal_blank_context_layer_is_treated_as_absent():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "output",
                                       context_layer="   \n  ")
    assert verdict is not None and error is None
    assert "CONTEXT AVAILABLE TO THE WORKER" not in provider.last_plan_prompt


def test_verify_goal_still_reports_unverified_when_all_tiers_fail_with_context_present():
    # WS1 invariant unchanged: verdict None still means unverified, even with a context_layer given.
    provider = LayeredScriptedProvider(verdicts=[{"unexpected": "shape"}, {"unexpected": "shape"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "output",
                                       context_layer="SOME_CONTEXT")
    assert verdict is None
    assert error and "met" in error


def test_verify_goal_met_and_not_met_verdicts_still_honored_with_context_present():
    provider = LayeredScriptedProvider(verdicts=[{"met": False, "reason": "missing X"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "output",
                                       context_layer="SOME_CONTEXT")
    assert error is None
    assert verdict is not None and verdict["met"] is False and verdict["reason"] == "missing X"


# --------------------------------------------------------------------------- #
# (b) Truncation: tail dropped, head kept, noted; env override; disable via <=0
# --------------------------------------------------------------------------- #

def test_truncate_verify_context_leaves_short_text_untouched():
    text = "short text well under the cap"
    assert truncate_verify_context(text, max_chars=1000) == text


def test_truncate_verify_context_drops_only_the_tail_and_notes_it():
    head = "HEAD" * 10
    tail = "TAIL" * 10
    text = head + tail
    out = truncate_verify_context(text, max_chars=len(head))
    assert out.startswith(head)
    assert tail not in out
    assert "truncated" in out
    assert str(len(tail)) in out  # names how many characters were dropped


def test_truncate_verify_context_is_a_byte_prefix_of_the_original():
    text = "0123456789" * 50
    out = truncate_verify_context(text, max_chars=37)
    assert text.startswith(out[:37])
    assert out[:37] == text[:37]


def test_truncate_verify_context_nonpositive_cap_disables_limit():
    text = "x" * 100000
    assert truncate_verify_context(text, max_chars=0) == text
    assert truncate_verify_context(text, max_chars=-5) == text


def test_verify_context_max_chars_env_wiring(monkeypatch):
    monkeypatch.delenv("QAR_VERIFY_CONTEXT_MAX_CHARS", raising=False)
    assert verify_context_max_chars() == 24000
    monkeypatch.setenv("QAR_VERIFY_CONTEXT_MAX_CHARS", "500")
    assert verify_context_max_chars() == 500
    monkeypatch.setenv("QAR_VERIFY_CONTEXT_MAX_CHARS", "not-a-number")
    assert verify_context_max_chars() == 24000  # bad value falls back, never raises
    monkeypatch.setenv("QAR_VERIFY_CONTEXT_MAX_CHARS", "0")
    assert verify_context_max_chars() == 24000  # non-positive env value also falls back to default


def test_verify_goal_truncates_an_oversized_context_layer_by_default(monkeypatch):
    monkeypatch.delenv("QAR_VERIFY_CONTEXT_MAX_CHARS", raising=False)
    provider = LayeredScriptedProvider(verdicts=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    head = "H" * 24000
    oversized = head + ("T" * 5000)
    verdict, error = orch._verify_goal("the goal", "the brief", "output", context_layer=oversized)
    assert verdict is not None and error is None
    cache_texts = cache_blocks_text(provider.verify_layers_calls[0])
    assert len(cache_texts) == 1
    assert cache_texts[0].startswith(head)
    assert "T" * 5000 not in cache_texts[0]
    assert "truncated" in cache_texts[0]


def test_verify_goal_context_layer_env_cap_override(monkeypatch):
    monkeypatch.setenv("QAR_VERIFY_CONTEXT_MAX_CHARS", "20")
    try:
        provider = LayeredScriptedProvider(verdicts=[{"met": True, "reason": "done"}])
        orch = make_orch(provider)
        text = "ABCDEFGHIJ" * 5  # 50 chars, over the 20-char cap
        verdict, error = orch._verify_goal("the goal", "the brief", "output", context_layer=text)
        assert verdict is not None and error is None
        cache_texts = cache_blocks_text(provider.verify_layers_calls[0])
        assert cache_texts[0].startswith("ABCDEFGHIJ" * 2)
        assert "truncated" in cache_texts[0]
    finally:
        monkeypatch.delenv("QAR_VERIFY_CONTEXT_MAX_CHARS", raising=False)


# --------------------------------------------------------------------------- #
# (c) Integration: verify's L2 traces to what the worker/answer call actually saw
# --------------------------------------------------------------------------- #

def test_deep_goal_verify_context_layer_matches_the_workers_own_context():
    """The deep-goal loop's verify call must carry the SAME context the deep worker itself received
    in its ``context_preamble`` -- not the turn-level plan context, which the per-goal closure does
    not even have in scope (see the per-goal context design in _assemble_for_goal_with_cards)."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = LayeredScriptedProvider(plans=[plan], verdicts=[{"met": True, "reason": "done"}])
    runner = RecordingRunner([DeepResult(met=True, output="did it")])
    assembler = PerGoalAssembler({"Implement feature X": "GOAL_CTX_TEXT_UNIQUE_998"})

    res = make_orch(provider, deep_runner=runner, context_assembler=assembler,
               config=OrchestratorConfig(deep_goal_max_iterations=3)).run("build X")

    assert res.kind == "deep"
    assert len(provider.verify_layers_calls) == 1
    cache_texts = cache_blocks_text(provider.verify_layers_calls[0])
    assert len(cache_texts) == 1
    verify_l2 = cache_texts[0]

    worker_preamble = runner.calls[0]["context_preamble"] or ""
    assert "GOAL_CTX_TEXT_UNIQUE_998" in verify_l2
    # Byte-identical to (a substring of, since the preamble may fold in more parts) what the worker
    # actually received -- never a re-render.
    assert verify_l2 in worker_preamble


def test_deep_goal_verify_context_layer_stable_across_retry_attempts():
    """The per-goal context is built ONCE and stays fixed across retries of the same goal, so
    repeated verify calls for the SAME goal share one byte-identical L2 (the cache-friendly
    property), even though the worker output and verdict differ each attempt."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = LayeredScriptedProvider(
        plans=[plan],
        verdicts=[{"met": False, "reason": "not there yet"}, {"met": True, "reason": "now done"}])
    runner = RecordingRunner([
        DeepResult(met=False, output="partial", error="incomplete"),
        DeepResult(met=True, output="done"),
    ])
    assembler = PerGoalAssembler({"Implement feature X": "STABLE_GOAL_CTX"})

    res = make_orch(provider, deep_runner=runner, context_assembler=assembler,
               config=OrchestratorConfig(deep_goal_max_iterations=3)).run("build X")

    assert res.kind == "deep"
    assert len(provider.verify_layers_calls) == 2
    l2_first = cache_blocks_text(provider.verify_layers_calls[0])
    l2_second = cache_blocks_text(provider.verify_layers_calls[1])
    assert l2_first == l2_second


def test_answer_verify_context_layer_matches_the_answers_own_grounding_layer():
    """The answer-verification loop's verify call must carry the SAME L2 the answer call it is
    judging used: ``grounding_context_layer(context_view)``, not a re-render of context_view.

    ``rationale`` is left unset on the plan: when present, ``_gen_answer`` folds a "PLANNER ANALYSIS"
    block into the SAME variable it renders as L2 (a pre-existing, unrelated quirk of the answer
    path), which would make the answer call's own L2 diverge from plain ``context_view`` and is
    orthogonal to what this test is proving."""
    provider = LayeredScriptedProvider(
        plans=[{"action": "answer", "model_tier": "sonnet"}],
        verdicts=[{"met": True, "reason": "good answer"}],
        answer_replies=["CONDITION: reply states the fact", "The real grounded answer."],
    )
    retrieval = StubRetrieval({"README.md": "the fact"})

    res = make_orch(provider, retrieval=retrieval).run("What's the deal?",
                                                    context_view="SOME_CONTEXT_VIEW_TEXT")
    assert res.kind == "answer"

    # The Fix-13 goal-condition derivation call does not build layers (no context to render there);
    # the real grounded answer is the one whose layers carry a cache-eligible context block.
    real_answer_layers = [l for l in provider.answer_layers_calls if cache_blocks_text(l)]
    assert real_answer_layers, "the real answer must have gone through the layered path"
    answer_l2 = cache_blocks_text(real_answer_layers[-1])[0]
    assert "SOME_CONTEXT_VIEW_TEXT" in answer_l2
    assert answer_l2 == grounding_context_layer("SOME_CONTEXT_VIEW_TEXT")

    assert provider.verify_layers_calls, "verify must have run (verify_claims defaults on)"
    verify_l2 = cache_blocks_text(provider.verify_layers_calls[-1])[0]
    assert verify_l2 == answer_l2


def test_answer_verify_context_layer_stable_across_regenerate_iterations():
    """A not-met verdict triggers a regeneration; the SECOND verify call's L2 must still match the
    (unchanged) context_view, byte-identical to the first verify call's L2."""
    provider = LayeredScriptedProvider(
        plans=[{"action": "answer", "rationale": "have it", "model_tier": "sonnet"}],
        verdicts=[{"met": False, "reason": "too vague", "next_action": "be specific"},
                 {"met": True, "reason": "good now"}],
        answer_replies=["CONDITION: be specific", "vague answer", "specific answer"],
    )
    res = make_orch(provider,
               config=OrchestratorConfig(answer_goal_max_iterations=3)).run(
        "What's the deal?", context_view="STABLE_CONTEXT_VIEW")
    assert res.kind == "answer"
    assert len(provider.verify_layers_calls) == 2
    l2_first = cache_blocks_text(provider.verify_layers_calls[0])
    l2_second = cache_blocks_text(provider.verify_layers_calls[1])
    assert l2_first == l2_second == [grounding_context_layer("STABLE_CONTEXT_VIEW")]


def test_deep_goal_with_no_assembler_wired_passes_no_context_layer():
    """When nothing is wired (no context_assembler), per_goal_context is "" and verify's context
    block is simply absent -- never a crash, never a stray empty CONTEXT section."""
    plan = {"action": "deep", "goal": "Just do it",
            "deep_subtasks": [{"goal": "Do it", "brief": "do it"}], "rationale": "deep"}
    provider = LayeredScriptedProvider(plans=[plan], verdicts=[{"met": True, "reason": "done"}])
    runner = RecordingRunner([DeepResult(met=True, output="done")])

    res = make_orch(provider, deep_runner=runner).run("do it")

    assert res.kind == "deep"
    assert cache_blocks_text(provider.verify_layers_calls[0]) == []
