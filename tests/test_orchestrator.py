"""Core loop: plan -> read -> re-plan -> answer, plus deep / confirm / cap fallback."""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


def test_plan_read_then_answer():
    # Step 1: planner says read README; step 2: planner answers.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "model_tier": "sonnet",
         "rationale": "need the doc"},
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact: pricing is $9/mo."})
    res = _orch(provider, retrieval).run("What's the price?")

    assert res.kind == "answer"
    assert retrieval.read_calls == ["README.md"]      # it actually read before answering
    assert provider.plan_calls == 2
    assert provider.answer_calls == 1
    # The README content was injected into the grounding the answer saw.
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "pricing is $9/mo" in joined


def test_chitchat_answers_without_reading():
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    retrieval = StubRetrieval({"README.md": "x"})
    res = _orch(provider, retrieval).run("thanks!")
    assert res.kind == "answer"
    assert retrieval.read_calls == []                 # answered with no reads


def test_grep_then_read_then_answer():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"grep": "pricing"}], "rationale": "locate"},
        {"action": "read", "reads": [{"rel_path": "docs/pricing.md"}], "rationale": "read it"},
        {"action": "answer", "rationale": "answer"},
    ])
    retrieval = StubRetrieval({"docs/pricing.md": "pricing: $9"})
    res = _orch(provider, retrieval).run("price?")
    assert res.kind == "answer"
    assert retrieval.grep_calls == ["pricing"]
    assert retrieval.read_calls == ["docs/pricing.md"]


def test_deep_runs_goal():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the one-pager", "deep_brief": "do it",
         "model_tier": "opus", "rationale": "real work"},
    ])
    runner = StubDeepRunner(met=True, output="one-pager written")
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("write the one-pager")
    assert res.kind == "deep"
    assert len(res.deep_results) == 1
    assert res.deep_results[0].met is True
    assert runner.calls[0]["goal"] == "Write the one-pager"
    # opus tier resolved to a concrete model id passed to the runner.
    assert "opus" in runner.calls[0]["model"]


def test_deep_fanout_runs_subtasks_in_parallel():
    provider = StubProvider(decisions=[
        {"action": "deep", "deep_subtasks": [
            {"goal": "A", "brief": "a"}, {"goal": "B", "brief": "b"}],
         "rationale": "split"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("do A and B")
    assert res.kind == "deep"
    assert len(res.deep_results) == 2
    assert {c["goal"] for c in runner.calls} == {"A", "B"}


def test_confirm_raises_escalation_and_returns_decision_id():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Buy item X for $50?", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_abc")
    res = _orch(provider, StubRetrieval(), escalation=sink).run("buy item X",
                                                                quest_id="quest_1")
    assert res.kind == "confirm"
    assert res.decision_id == "dec_abc"
    assert res.question == "Buy item X for $50?"
    assert sink.raised[0].quest_id == "quest_1"
    assert sink.raised[0].default_on_silence == "hold"


def test_cap_falls_back_to_best_effort_answer():
    # Planner keeps saying "read" forever -> hit max_steps -> best-effort grounded answer.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "again"}
        for _ in range(10)
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING content"})
    res = _orch(provider, retrieval, config=OrchestratorConfig(max_steps=3)).run("q")
    assert res.kind == "answer"
    assert res.partial is True
    assert res.steps == 3


class _EmptyRetrieval:
    """A retrieval adapter whose reads/greps yield NOTHING (no observation at all), so a
    capped loop with no usable gather escalates to a deep run."""
    def read_section(self, *a, **k):
        return None
    def grep(self, *a, **k):
        return None
    def query(self, spec):
        return None


def test_cap_with_nothing_gathered_escalates_to_deep():
    # Planner keeps asking to read, but nothing comes back -> nothing gathered -> escalate to deep.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "x.md"}], "rationale": "again"}
        for _ in range(10)
    ])
    runner = StubDeepRunner(met=True, output="did it")
    res = Orchestrator(
        retrieval=_EmptyRetrieval(), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner, config=OrchestratorConfig(max_steps=2),
    ).run("hard thing")
    assert res.kind == "deep"
    assert res.deep_results and res.deep_results[0].met is True
