"""Core loop: plan -> read -> re-plan -> answer, plus deep / confirm / cap fallback."""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    _render_gathered,
    _render_gathered_for_planner,
    _summarize_observation,
)

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
    # plan_calls = 2 loop steps + the post-answer verification (answer_goal_max_iterations): the
    # stub's exhausted-queue reply has no "met" key, so the verifier makes its verify_tier call and
    # then ONE planner-tier fallback retry (see _verify_goal's tier ladder) = 2 verify calls.
    assert provider.plan_calls == 4
    # +1 for Fix 13's always-on cheap goal-condition derivation call (STAGE 1), +1 real answer.
    assert provider.answer_calls == 2
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
        {"met": True, "reason": "one-pager written"},  # goal verification
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


def test_planner_confirm_is_honored_not_second_guessed_by_keywords():
    # QAR must NOT gate a human-in-the-loop confirm on substring matches against the planner's own
    # confirm question. The old `_confirm_is_redundant` / `_CONFIRM_FORK_MARKERS` denylist did
    # exactly that and silently leaked outward-facing verbs (share/invite/grant/…). It was removed:
    # when the planner decides an act needs confirmation, that decision is honored and surfaces as a
    # decision-request, regardless of the wording of the request or the confirm question.
    provider = StubProvider(decisions=[
        {"action": "confirm",
         "confirm_question": "I will share this doc with the whole team. Approve?",
         "rationale": "outward-facing"},
    ])
    runner = StubDeepRunner(met=True, output="should not run")
    sink = StubEscalation(decision_id="dec_share")
    res = _orch(provider, StubRetrieval(), deep_runner=runner, escalation=sink).run(
        "share this doc with the team", quest_id="quest_1")
    assert res.kind == "confirm", "a planner confirm must surface as a confirm, not auto-execute"
    assert res.decision_id == "dec_share"
    assert sink.raised, "a decision-request must be raised for a planner confirm"
    assert not runner.calls, "a confirm must not silently execute via the deep runner"


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
        for _ in range(2)  # matches max_steps below; escalation to deep is auto-derived, not planned
    ] + [{"met": True, "reason": "did it"}])  # goal verification for the escalated deep run
    runner = StubDeepRunner(met=True, output="did it")
    res = Orchestrator(
        retrieval=_EmptyRetrieval(), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner, config=OrchestratorConfig(max_steps=2),
    ).run("hard thing")
    assert res.kind == "deep"
    assert res.deep_results and res.deep_results[0].met is True


def test_answer_describing_unexecuted_work_escalates_to_deep():
    # Regression guard: a cheap planner ends a code-fix request with action="answer" and FORGETS
    # to set answer_contains_work_to_execute. The answer only DESCRIBES the fix ("to fix this, I
    # need to update ..."). Without the unexecuted-work safety net the turn just finishes having
    # talked about the change instead of doing it. The orchestrator must auto-escalate to a deep
    # run that actually applies the work.
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "describe"}],
        answer_text="To fix this, I need to update the date-assignment logic in the code.",
    )
    runner = StubDeepRunner(met=True, output="applied the date fix")
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "the system incorrectly assigns dates to actions"
    )
    # The descriptive answer is still returned, but the work was actually executed via a deep run.
    assert res.kind == "answer"
    assert runner.calls, "expected a deferred deep run to execute the described work"


def test_unexecuted_work_classifier_catches_inability_confessions():
    # Caught live (2026-07-19 reliability battery): a write-a-file task was answered with an
    # explicit inability confession + a plan for "the system" to run, and reported done. The
    # classifier missed both shapes; they must register as unexecuted work.
    from quest_ai_runner.core.orchestrator import _answer_describes_unexecuted_work as describes

    assert describes(
        "I cannot execute this task in the read-and-answer step -- I can't run commands or "
        "edit files here. The system will need to execute the following: run the command "
        "and write the file."
    )
    assert describes("The system will need to write the branch name to the file.")
    # User-dependent asks and plain informational answers must stay False.
    assert not describes("Please provide your API key so I can continue.")
    assert not describes("The current branch is july.")


def test_deferred_deep_output_is_folded_into_final_answer():
    # Regression: a deferred deep run did the real work, but the user-facing reply stayed the
    # pre-deep proposal (which reads as "shall I proceed?") with NO awareness of what the deep run
    # produced — the deliverable was emitted only as a side milestone (and truncated by some
    # consumers). The final answer must be RE-SYNTHESIZED grounded in the deep run's output, so the
    # user sees the real deliverable, not a stale proposal.
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "describe"}],
        answer_text="I need to update the architecture; let me know if you want me to proceed.",
    )
    deliverable = "MULTILANG_PLAN_DELIVERABLE: phase 1 extract strings, phase 2 Spanish locale."
    runner = StubDeepRunner(met=True, output=deliverable)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "the system incorrectly assigns dates to actions"
    )
    assert res.kind == "answer"
    assert runner.calls, "expected a deferred deep run to execute the work"
    # The FINAL answer call (the synthesis) must be grounded in the deep output and use the
    # after-deep synthesis prompt — proving the reply now reflects what was actually produced.
    joined = "\n".join(
        (m["content"] if isinstance(m["content"], str) else str(m["content"]))
        for m in provider.last_answer_messages
    )
    assert deliverable in joined, "final answer was not grounded in the deep run's output"
    assert "ACTUAL RESULT OF THE WORK YOU JUST DID" in joined, "expected the after-deep synthesis path"
    assert provider.answer_calls >= 2  # pre-deep proposal + post-deep synthesis


def test_discovery_listing_is_not_answer_grounding_content():
    # A capability MENU (list_operations and friends) must inform the planner but NEVER be presented
    # to the answer LLM as content to answer from — otherwise the model answers from the menu ("I
    # have these operations, shall I run discovery?") instead of gathering real material.
    from quest_ai_runner.core.orchestrator import _grounding_block, _render_gathered

    discovery = {"kind": "query", "locator": "list_operations", "discovery": True,
                 "text": "OPERATION: discover_goals — list goals"}
    content = {"kind": "read", "rel_path": "i18n.md", "locator": "head",
               "text": "REAL_FILE_CONTENT: the app uses an i18n string table."}

    # Answer grounding: the menu is excluded; the real file content is kept.
    block = _grounding_block("", [discovery, content], False)
    assert "REAL_FILE_CONTENT" in block
    assert "OPERATION: discover_goals" not in block
    assert "ACTUAL CONTENT READ FOR THIS ANSWER" in block  # the real content section is present

    # When ONLY a discovery menu was gathered, there is NO answer-content section at all (so the
    # answer LLM grounds on nothing and says so, rather than answering from the menu).
    only_menu = _grounding_block("", [discovery], False)
    assert "ACTUAL CONTENT READ FOR THIS ANSWER" not in only_menu

    # The planner-facing render labels the menu as capabilities, not as gathered facts.
    planner_view = _render_gathered([discovery])
    assert "AVAILABLE CAPABILITIES" in planner_view
    assert "NOT" in planner_view  # explicitly flagged as not content/answer


def test_specificity_gate_is_woven_into_planner_deep_and_grounding():
    # Regression guard for the "answered about a sibling topic" failure: a question about one
    # specific subject ("result-prediction evaluation") must not be answered from a DIFFERENT
    # subject's docs ("atom evaluation") just because both share a category word. The discipline
    # lives in context_doctrine.SPECIFICITY_GATE and must reach every layer that can ground an
    # answer: the planner prompt, the deep doctrine, and the answer-time grounding block.
    from quest_ai_runner.core import context_doctrine as cd
    from quest_ai_runner.core.orchestrator import PLANNER_PROMPT, _grounding_block

    # The gate states the primary discipline (match the specific referent, not its category) and
    # explicitly subordinates recency to it (product owner: relevance/specificity first, time only backup).
    gate = cd.SPECIFICITY_GATE
    assert "sibling" in gate.lower()
    assert "category" in gate.lower()
    assert "NEVER overrides specificity" in gate  # recency is a backup, never an override

    # Gate carries no literal braces, so the assembled planner prompt still .format()s cleanly.
    assert "{" not in gate and "}" not in gate
    assert "SPECIFICITY" in PLANNER_PROMPT
    assert "SPECIFICITY" in cd.DEEP_CONTEXT_DOCTRINE

    # The answer-time grounding block (where the wrong-subject answer was actually produced) now
    # tells the model to ground ONLY in the asked subject and to flag, not answer from, a sibling.
    block = _grounding_block("ctx", [], False)
    assert "sibling topic" in block
    assert "specifically about what was asked" in block


class _GCard:
    def __init__(self, id, title, relevance, body):
        self.id, self.title, self.relevance, self.body = id, title, relevance, body


class _StubGuidance:
    """Minimal GuidanceProvider: select() returns fixed quality-standard cards."""
    def __init__(self, cards):
        self._cards = cards

    def select(self, message, *, team_id=None, org_id=None, limit=3):
        return self._cards


def test_answer_verified_against_goal_regenerates_when_not_met():
    # Top-tier goal loop on a plain ANSWER: when a quality bar (guidance) is wired, the answer is
    # verified against the goal and REGENERATED with steering if it falls short.
    card = _GCard("g1", "Quality bar", "always", "Be specific and complete.")
    provider = StubProvider(
        decisions=[
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
            {"met": False, "reason": "too vague", "next_action": "add the specifics"},
        ],
        answer_text="a vague answer",
    )
    res = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                       guidance=_StubGuidance([card]),
                       config=OrchestratorConfig(answer_goal_max_iterations=2)).run("explain X")
    assert res.kind == "answer"
    # +1 for Fix 13's always-on cheap goal-condition derivation call (STAGE 1), +1 real answer,
    # +1 regenerated after verify said not-met.
    assert provider.answer_calls == 3


def test_answer_verified_met_no_regeneration():
    card = _GCard("g1", "Quality bar", "always", "Be specific.")
    provider = StubProvider(
        decisions=[
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
            {"met": True, "reason": "complete and specific"},
        ],
        answer_text="a complete, specific answer",
    )
    res = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                       guidance=_StubGuidance([card]),
                       config=OrchestratorConfig(answer_goal_max_iterations=2)).run("explain X")
    assert res.kind == "answer"
    # +1 for Fix 13's always-on cheap goal-condition derivation call (STAGE 1), +1 real answer
    # verified met on the first try, no wasted regeneration.
    assert provider.answer_calls == 2


def test_answer_not_verified_without_a_quality_bar():
    # No GuidanceProvider wired => no quality bar => no answer verification (single generation).
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
        answer_text="an answer",
    )
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(answer_goal_max_iterations=2)).run("explain X")
    assert res.kind == "answer"
    # +1 for Fix 13's always-on cheap goal-condition derivation call (STAGE 1), +1 real answer.
    assert provider.answer_calls == 2


def test_answer_goal_verifier_none_retries_not_silently_accepts():
    # A None verdict (verifier call fails — no "met" key in response) must RETRY, not silently
    # accept the answer. With max_iterations=3: attempt 1 returns None (retry), attempt 2 returns
    # met=True. Result: 2 plan calls for verification, 1 answer generation (no regeneration needed
    # since met=True on retry).
    card = _GCard("g1", "Quality bar", "always", "Be specific.")
    provider = StubProvider(
        decisions=[
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
            {},  # no "met" key -> _verify_goal returns None -> should retry, not accept
            {"met": True, "reason": "complete on retry"},
        ],
        answer_text="Let me check the files and pull up the logs...",
    )
    res = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                       guidance=_StubGuidance([card]),
                       config=OrchestratorConfig(answer_goal_max_iterations=3)).run("what's the state?")
    assert res.kind == "answer"
    # 1 plan call (action=answer) + 2 verify calls (None then met=True) = 3 total
    assert provider.plan_calls == 3


def test_answer_goal_verifier_none_exhausted_accepts():
    # If ALL verify calls return None (verifier always fails), best-effort accept after exhausting
    # attempts rather than blocking the turn.
    card = _GCard("g1", "Quality bar", "always", "Be specific.")
    provider = StubProvider(
        decisions=[
            {"action": "answer", "model_tier": "sonnet", "rationale": "answer"},
            {},  # None verdict on only attempt (max_iterations=2 means 1 verify call)
        ],
        answer_text="Let me pull up the data...",
    )
    res = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                       guidance=_StubGuidance([card]),
                       config=OrchestratorConfig(answer_goal_max_iterations=2)).run("what's the state?")
    assert res.kind == "answer"  # accepted after exhausting attempts, turn must complete


def test_verify_goal_prompt_rejects_future_intent():
    # The VERIFY_GOAL_PROMPT must explicitly instruct the verifier that future-intent language
    # ("Let me check", "I'm pulling up", etc.) is met=false.
    from quest_ai_runner.core.orchestrator import VERIFY_GOAL_PROMPT
    assert "Let me check" in VERIFY_GOAL_PROMPT
    assert "pulling" in VERIFY_GOAL_PROMPT  # "I'm pulling up" may wrap
    assert "Future intent is NOT a result" in VERIFY_GOAL_PROMPT
    assert "met=false" in VERIFY_GOAL_PROMPT


def test_goal_loop_iterates_until_verified_met():
    # Our own goal loop (replaces Claude Code /goal): the worker runs, the brain VERIFIES the
    # done-standard, and if not met it re-runs with steering. Here verify says not-met on attempt 1,
    # met on attempt 2 -> two worker runs, final met=True, and the 2nd brief carries the steering.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},  # planner
        {"met": False, "reason": "the fix was incomplete", "next_action": "also update the helper"},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="made an edit")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=3)).run("fix the thing")
    assert res.kind == "deep"
    assert len(runner.calls) == 2, "should have re-run once after verify said not-met"
    assert res.deep_results[0].met is True
    assert "also update the helper" in runner.calls[1]["brief"]  # steering fed into retry


class _TokenDeepRunner:
    """A deep runner that records the model used per call and reports a fixed token count, so the
    model-ladder escalation and the token budget can be asserted."""
    def __init__(self, tokens=1000):
        self.calls = []
        self._tokens = tokens

    def run_goal(self, *, goal, brief, model=None, max_turns=None):
        from quest_ai_runner.core.adapters import DeepResult
        self.calls.append({"goal": goal, "brief": brief, "model": model})
        return DeepResult(met=True, output="attempted", tokens=self._tokens)


def test_deep_loop_escalates_model_on_not_met():
    # Verify keeps saying not-met -> the worker model escalates through the ladder fast->strong.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": False, "reason": "no", "next_action": "x"},
        {"met": False, "reason": "no", "next_action": "x"},
        {"met": False, "reason": "no", "next_action": "x"},
    ])
    runner = _TokenDeepRunner(tokens=10)
    _orch(provider, StubRetrieval(), deep_runner=runner,
          config=OrchestratorConfig(deep_goal_max_iterations=3,
                                    deep_model_ladder=["haiku", "sonnet", "opus"],
                                    deep_goal_token_budget=None)).run("fix it")
    assert [c["model"] for c in runner.calls] == ["haiku", "sonnet", "opus"]  # escalated each retry


class _RunIdCapturingDeepRunner:
    """A deep runner whose ``run_goal`` accepts ``run_id`` and records it per call, so a test can
    assert the orchestrator passes the SAME id across retries of one subgoal."""
    def __init__(self, met: bool = True, output: str = "attempted"):
        self._met = met
        self._output = output
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, run_id=None):
        from quest_ai_runner.core.adapters import DeepResult
        self.calls.append({"goal": goal, "brief": brief, "model": model, "run_id": run_id})
        return DeepResult(met=self._met, output=self._output)


def test_deep_retry_reuses_the_same_run_id():
    # A retry spawns a brand-new subprocess/session under the hood, but it is still the SAME
    # subgoal -- the orchestrator must pass the same run_id on every attempt so a consumer's
    # dashboard doesn't render each retry as a duplicate deep-run entry.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": False, "reason": "no", "next_action": "x"},
        {"met": True, "reason": "done"},
    ])
    runner = _RunIdCapturingDeepRunner()
    _orch(provider, StubRetrieval(), deep_runner=runner,
          config=OrchestratorConfig(deep_goal_max_iterations=3)).run("fix it")
    assert len(runner.calls) == 2, "should have re-run once after verify said not-met"
    run_ids = [c["run_id"] for c in runner.calls]
    assert run_ids[0] is not None
    assert run_ids[0] == run_ids[1], "retries of the same subgoal must share one stable run_id"


def test_deep_loop_stops_at_token_budget():
    # Each attempt reports 1000 tokens; a 1500-token budget allows attempt 1, then stops after the
    # second attempt pushes cumulative tokens past the budget (not the full attempt cap).
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
    ] + [{"met": False, "reason": "no", "next_action": "x"} for _ in range(8)])
    runner = _TokenDeepRunner(tokens=1000)
    _orch(provider, StubRetrieval(), deep_runner=runner,
          config=OrchestratorConfig(deep_goal_max_iterations=8,
                                    deep_model_ladder=["haiku"],
                                    deep_goal_token_budget=1500)).run("fix it")
    assert len(runner.calls) == 2  # stopped on budget, not the 8-attempt cap


def test_planner_list_output_does_not_crash():
    # A provider can return a LIST instead of a dict (multiple tool calls / a JSON array). The
    # planner must coerce it rather than raise 'list' object has no attribute 'get' and fall over.
    from quest_ai_runner.core.orchestrator import normalize_decision, OrchestratorConfig
    cfg = OrchestratorConfig()
    assert normalize_decision([{"action": "deep", "goal": "g", "rationale": "r"}], cfg).action == "deep"
    assert normalize_decision(["weird"], cfg).action == "answer"   # no dict in list -> safe default
    assert normalize_decision("nope", cfg).action == "answer"      # non-dict scalar -> safe default


def test_input_inbox_auto_drains_into_deep_run():
    # The generic abstraction: an interface pushes a mid-run message to the wired inbox; the
    # orchestrator auto-drains this conversation (no explicit pending_inputs) and folds it in.
    from quest_ai_runner.core.inbox import InMemoryInbox
    inbox = InMemoryInbox()
    inbox.push("conv1", "also handle nulls")
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="working")
    res = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                       deep_runner=runner, input_inbox=inbox,
                       config=OrchestratorConfig(deep_goal_max_iterations=2)).run("fix it", quest_id="conv1")
    assert res.kind == "deep"
    assert "also handle nulls" in runner.calls[0]["brief"]  # drained + folded with no manual wiring


def test_deep_run_folds_in_new_user_messages():
    # New messages the user sends mid-run are folded into the next deep process (here, the retry),
    # so a long-running goal loop acts on the latest input, not just the stale original request.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": False, "reason": "incomplete", "next_action": "keep going"},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="working")
    new_msgs = ["also handle the edge case"]
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=3)).run(
        "fix the thing", pending_inputs=lambda: new_msgs)
    assert res.kind == "deep"
    assert len(runner.calls) == 2
    # The 2nd process (retry) brief includes the new user message folded in.
    assert "also handle the edge case" in runner.calls[1]["brief"]


def test_goal_loop_stops_when_first_attempt_verified_met():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": True, "reason": "done on first try"},
    ])
    runner = StubDeepRunner(met=True, output="made the edit")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=3)).run("fix the thing")
    assert len(runner.calls) == 1  # verified met immediately, no wasted retries
    assert res.deep_results[0].met is True


def test_goal_loop_reports_not_met_after_exhausting_attempts():
    # Verify keeps saying not-met -> after max iterations the result is a confirmed failure.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": False, "reason": "still wrong", "next_action": "try again"},
        {"met": False, "reason": "still wrong", "next_action": "try again"},
    ])
    runner = StubDeepRunner(met=True, output="attempted something")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=2)).run("fix the thing")
    assert len(runner.calls) == 2
    assert res.deep_results[0].met is False  # brain-verified not met, despite worker exit-0


def test_deep_goal_verifier_none_is_never_trusted_as_done():
    # WS1 fix: if the verifier can't produce a usable verdict (both verify_tier and its
    # planner_tier fallback return no "met" key), the loop must NOT fall back to trusting the
    # worker's raw exit-code success (StubDeepRunner reports met=True unconditionally) -- that was
    # exactly the "verifier outage silently re-opens 'said Completed but did nothing'" bug. A None
    # verdict means UNVERIFIED, never a silent "done", and the reported error names why.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {},  # verify_tier call: unusable (no "met" key)
        {},  # planner_tier fallback: also unusable -> verdict is None
    ])
    runner = StubDeepRunner(met=True, output="worker claims success")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=3)).run("fix the thing")
    assert len(runner.calls) == 1  # no blind retry on an unverifiable result
    assert res.deep_results[0].met is False, "an unverified result must never be reported as done"
    assert "unverified" in (res.deep_results[0].error or "").lower()


def test_confirm_condenses_long_summary_before_escalation():
    # A decision summary is stored by Quest as a goal CONDITION (4000-char cap). A verbose planner
    # question must NOT be dumped there raw; it is condensed by an LLM call into a short ask first.
    long_q = "Detailed background analysis of the date logic. " * 400  # ~19k chars of raw text
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": long_q, "rationale": "ambiguous"},
    ])
    sink = StubEscalation(decision_id="dec_x")
    res = _orch(provider, StubRetrieval(), escalation=sink).run("do the risky thing", quest_id="q1")
    assert res.kind == "confirm"
    assert sink.raised, "an escalation should have been raised"
    sent = sink.raised[0].summary
    assert len(sent) <= 600, f"decision summary not condensed: {len(sent)} chars"
    assert sent != long_q  # raw text was not passed through


def test_short_confirm_summary_passes_through_unchanged():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Send the donor email now?", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_y")
    _orch(provider, StubRetrieval(), escalation=sink).run("email the donor", quest_id="q2")
    assert sink.raised[0].summary == "Send the donor email now?"  # short -> untouched, no LLM call


def test_actionable_message_with_proposal_answer_escalates_to_deep():
    # The real-world failure the user hit: the planner answers (PROPOSES) a code change in one step
    # and forgets the explicit flag. The proposal phrasing ("Aligning these to use the same
    # created_at field will guarantee ...") matches NONE of the answer-text regex nets, so the turn
    # used to end as a proposal that never ran. The message-intent fallback (keyed off the stable
    # user message "the system incorrectly assigns dates ...") must still escalate to a deep run,
    # and the brief must carry the proposed approach so the deep run APPLIES it.
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "propose"}],
        answer_text=("Aligning these to use the same created_at field will guarantee that editing "
                     "an entry's time to yesterday immediately moves it out of today's actions."),
    )
    runner = StubDeepRunner(met=True, output="applied the change")
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "the system incorrectly assigns dates to actions"
    )
    assert res.kind == "answer"
    assert runner.calls, "message-intent fallback must execute the proposed change"
    assert "created_at" in runner.calls[0]["brief"], "brief should carry the proposed approach"


def test_plain_informational_answer_does_not_escalate():
    # The net must stay OFF for genuine Q&A: an informational answer with no described change
    # should NOT trigger a deep run (no false escalation / no wasted subprocess).
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "inform"}],
        answer_text="The cache refreshes every 12 hours via a cron job.",
    )
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("how often does the cache refresh?")
    assert res.kind == "answer"
    assert not runner.calls, "informational answer must not escalate to a deep run"


def test_message_requests_change_distinguishes_questions_from_commands():
    # Regression: a QUESTION that merely mentions an action verb ("how would I add X?") was being
    # auto-escalated into a task instead of answered. _message_requests_change must read INTENT:
    # questions -> False (answer), commands -> True (execute).
    from quest_ai_runner.core.orchestrator import _message_requests_change

    # COMMANDS (the user is directing the work) -> should execute.
    for cmd in [
        "fix the back button",
        "add a field to the form",
        "update my goal to be more ambitious",
        "refactor the date logic",
        "can you fix the date bug?",
        "could you add a measurable outcome?",
        "please update the endpoint",
        "the system incorrectly assigns dates to actions",  # bug report = implicit command
    ]:
        assert _message_requests_change(cmd) is True, f"command should escalate: {cmd!r}"

    # QUESTIONS (the user is asking ABOUT something, even with an action verb) -> should answer.
    for q in [
        "how would I add a new field to the form?",
        "what would it take to fix the back button?",
        "should we refactor the date logic?",
        "why does the build break on mobile?",
        "how does the date logic work?",
        "is it possible to add SSO?",
        "what's the best way to update a goal?",
        "do you think we should change this?",
    ]:
        assert _message_requests_change(q) is False, f"question should NOT escalate: {q!r}"


def test_question_with_change_verb_is_answered_not_executed():
    # End-to-end: the planner answers a question that happens to contain a change verb ("add"),
    # and the message-intent fallback must NOT turn it into a deep task.
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "inform"}],
        answer_text="You'd add it in the form schema, then wire it to the submit handler.",
    )
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "how would I add a new field to the form?"
    )
    assert res.kind == "answer"
    assert not runner.calls, "a question must be answered, never auto-executed as a task"


# --- per-step planner-view leaning (compress older gathered) --------------------------------

def _read_obs(path: str, body: str) -> dict:
    return {"kind": "read", "rel_path": path, "locator": "head", "text": body}


def test_planner_view_unchanged_below_threshold():
    # With few observations the planner view is byte-for-byte the full render (back-compat).
    gathered = [_read_obs(f"f{i}.md", f"body {i}") for i in range(3)]
    full = _render_gathered(gathered)
    lean = _render_gathered_for_planner(gathered, recent_full=4, compress_over=6)
    assert lean == full
    assert _render_gathered_for_planner([], recent_full=4, compress_over=6) == "[]"


def test_planner_view_compresses_older_keeps_recent_full():
    big = "X" * 500  # a long body so compression to a one-line summary is observable
    gathered = [_read_obs(f"f{i}.md", f"unique-body-{i} " + big) for i in range(10)]
    lean = _render_gathered_for_planner(gathered, recent_full=2, compress_over=6)
    full = _render_gathered(gathered)
    # Older observations collapse to one line (path + truncated head), so the verbose bodies drop.
    assert "EARLIER READS (8)" in lean
    assert len(lean) < len(full)
    assert lean.count(big) == 2               # only the 2 newest keep their full body
    assert full.count(big) == 10
    assert "f0.md" in lean                     # ...but every older source is still listed by path
    # The two newest are rendered in full (body visible).
    assert "unique-body-8" in lean
    assert "unique-body-9" in lean


def test_summarize_observation_grep_and_read():
    grep = {"kind": "grep", "pattern": "pricing", "scope": "docs",
            "hits": [{"rel_path": "a.md", "line_no": 1, "line": "x"},
                     {"rel_path": "b.md", "line_no": 2, "line": "y"}]}
    s = _summarize_observation(grep)
    assert "GREP 'pricing'" in s and "2 hit(s)" in s and "a.md" in s
    read = _read_obs("doc.md", "the body text here")
    assert "doc.md" in _summarize_observation(read)


def test_loop_feeds_lean_view_to_planner_on_replan():
    # Force a long read chain past the threshold, then assert the LAST plan prompt is the lean view
    # (older bodies compressed) while the final ANSWER grounding still sees every body.
    reads = [
        {"action": "read", "reads": [{"rel_path": f"f{i}.md"}], "rationale": "r"}
        for i in range(8)
    ]
    provider = StubProvider(decisions=reads + [{"action": "answer", "rationale": "done"}])
    big = "Z" * 400
    files = {f"f{i}.md": f"GROUNDING unique-body-{i} " + big for i in range(8)}
    retrieval = StubRetrieval(files)
    cfg = OrchestratorConfig(max_steps=12, planner_recent_full=2, planner_compress_over=4)
    res = _orch(provider, retrieval, config=cfg).run("question")
    assert res.kind == "answer"
    # Use plan_prompts[-3]: the last PLANNER prompt; plan_prompts[-2:] are the post-answer
    # verification calls (verify_tier + its planner-tier fallback on the stub's unusable verdict),
    # which use the VERIFY_GOAL_PROMPT, not the planner prompt — "EARLIER READS" is not there.
    last_prompt = provider.plan_prompts[-3]
    assert "EARLIER READS" in last_prompt          # leaning engaged on later steps
    # The oldest full bodies are compressed out of the planner view (only recent kept verbatim).
    assert last_prompt.count(big) <= cfg.planner_recent_full
    answer_grounding = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "unique-body-0" in answer_grounding        # ...but the answer still sees all of it
    assert answer_grounding.count(big) >= 7           # every read's full body is in the grounding


# --- cross-step repeat-context leaning (abbreviate unchanged transcript + context_view) ----------

_TRANSCRIPT = "USER: earlier thing\nASSISTANT: earlier reply\nUSER: the latest message"
_CONTEXT = "CONTEXT-MARKER: a long static context block that locates lots of content"


def _read_then_answer_provider():
    return StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "f.md"}], "rationale": "read"},
        {"action": "answer", "rationale": "answer"},
    ])


def test_repeat_context_off_resends_full_on_replan():
    # Default (knob off): every plan prompt — step 1 AND re-plan — carries the full transcript
    # + context_view, byte-for-byte the prior behavior.
    provider = _read_then_answer_provider()
    retrieval = StubRetrieval({"f.md": "GROUNDING body"})
    res = _orch(provider, retrieval).run(
        "q", transcript=_TRANSCRIPT, context_view=_CONTEXT)
    assert res.kind == "answer"
    # plan_prompts[0..1] are the 2 loop steps; plan_prompts[2..3] are the post-answer verification
    # calls (verify_tier + planner-tier fallback on the stub's unusable verdict) — not checked here.
    assert len(provider.plan_prompts) == 4
    for p in provider.plan_prompts[:2]:
        assert _CONTEXT in p
        assert "the latest message" in p             # full transcript present each step
    assert "unchanged since step 1" not in provider.plan_prompts[0]
    assert "unchanged since step 1" not in provider.plan_prompts[1]


def test_repeat_context_on_step1_full_replan_abbreviated():
    # Knob on: step 1 sees the full transcript + context; later re-plan steps see only the
    # reference notes, not the unchanged context again.
    provider = _read_then_answer_provider()
    retrieval = StubRetrieval({"f.md": "GROUNDING body"})
    cfg = OrchestratorConfig(planner_abbreviate_repeat_context=True)
    res = _orch(provider, retrieval, config=cfg).run(
        "q", transcript=_TRANSCRIPT, context_view=_CONTEXT)
    assert res.kind == "answer"
    # plan_prompts[0..1] are the 2 loop steps; plan_prompts[2..3] are the post-answer verification
    # calls (verify_tier + planner-tier fallback on the stub's unusable verdict) — not checked here.
    assert len(provider.plan_prompts) == 4
    step1, replan = provider.plan_prompts[0], provider.plan_prompts[1]
    # Step 1: full context + transcript.
    assert _CONTEXT in step1
    assert "the latest message" in step1
    assert "unchanged since step 1" not in step1
    # Re-plan: the unchanged context + transcript are replaced by reference notes.
    assert _CONTEXT not in replan
    assert "the latest message" not in replan
    assert "unchanged since step 1" in replan


def test_repeat_context_on_answer_still_gets_full_context():
    # Even with the knob on, the final ANSWER grounding must carry the full transcript + context.
    provider = _read_then_answer_provider()
    retrieval = StubRetrieval({"f.md": "GROUNDING body"})
    cfg = OrchestratorConfig(planner_abbreviate_repeat_context=True)
    res = _orch(provider, retrieval, config=cfg).run(
        "q", transcript=_TRANSCRIPT, context_view=_CONTEXT)
    assert res.kind == "answer"
    grounding = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert _CONTEXT in grounding                       # full context_view reaches the answer
    assert "the latest message" in grounding           # full transcript reaches the answer
    assert "unchanged since step 1" not in grounding   # no abbreviation leaks into the answer


def test_repeat_context_on_does_not_abbreviate_when_empty():
    # With no transcript/context, the knob is a no-op (no reference notes injected).
    provider = _read_then_answer_provider()
    retrieval = StubRetrieval({"f.md": "GROUNDING body"})
    cfg = OrchestratorConfig(planner_abbreviate_repeat_context=True)
    res = _orch(provider, retrieval, config=cfg).run("q")
    assert res.kind == "answer"
    for p in provider.plan_prompts:
        assert "unchanged since step 1" not in p


# --- direct follow-up: answer from the transcript instead of re-searching ------------------------

def test_planner_prompt_instructs_answering_followups_from_transcript():
    # The planner the model sees on a direct follow-up must carry BOTH the transcript that holds
    # the answer AND the explicit instruction to answer from it instead of re-searching the corpus.
    # Guards the fix for: "what's the filepath?" right after the AI described the plan + its file
    # triggering a fresh corpus search instead of answering from the conversation.
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "already in transcript"},
    ])
    transcript = ("USER: what's the current plan?\n"
                  "ASSISTANT: the plan lives at docs/PLAN_X.md and covers steps A, B, C")
    retrieval = StubRetrieval({"docs/PLAN_X.md": "should-not-be-read"})
    res = _orch(provider, retrieval).run("what's the filepath?", transcript=transcript)
    assert res.kind == "answer"
    assert retrieval.read_calls == []        # answered from the conversation, no re-search
    step0 = provider.plan_prompts[0]
    assert "docs/PLAN_X.md" in step0         # the answering fact was in front of the planner
    assert "ANSWER FROM THE CONVERSATION WHEN IT'S ALREADY THERE" in step0
    assert "DIRECT FOLLOW-UP" in step0


# ---------------------------------------------------------------------------
# Per-run model hint (model_hint= kwarg on run / run_stream).
# ---------------------------------------------------------------------------

class _ModelCapturingProvider(StubProvider):
    """StubProvider that records the exact model id passed to plan() and answer()."""
    def __init__(self, decisions):
        super().__init__(decisions)
        self.answer_models: list = []
        self.plan_models: list = []

    def plan(self, prompt, *, model, tool_schema):
        self.plan_models.append(model)
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model, system=None):
        self.answer_models.append(model)
        return super().answer(messages, model=model, system=system)


def test_model_hint_overrides_planner_tier_on_answer():
    """model_hint flows through to provider.answer — the model id differs from the default."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "ok"},
    ])
    retrieval = StubRetrieval({"f.md": "x"})
    # The hint "opus" should cause the registry to resolve "opus" instead of "haiku".
    res = _orch(provider, retrieval).run("q", model_hint="opus")
    assert res.kind == "answer"
    # The model passed to provider.answer must be the registry's "opus" resolution.
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_opus = registry.resolve_tier("opus")
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier (never the hint) before the real, hinted answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_opus]


def test_model_hint_does_not_touch_planner_calls():
    """The hint applies to answer/deep steps ONLY: the planner's own structured calls stay on
    the cheap configured planner tier (deliberate — a hint must not make planning expensive)."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "rationale": "ok"},
    ])
    retrieval = StubRetrieval({"f.md": "x"})
    res = _orch(provider, retrieval).run("q", model_hint="opus")
    assert res.kind == "answer"
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_planner = registry.resolve_tier("balanced")  # OrchestratorConfig.planner_tier default
    # plan_models[0] is the PLANNER call — the one the hint must not touch. Later plan calls are
    # the post-answer goal verification, which deliberately runs at verify_tier ("best"), hint or
    # not, so they are not part of this assertion.
    assert provider.plan_models[0] == expected_planner


def test_model_hint_absent_leaves_planner_tier_unchanged():
    """Without model_hint, the planner's model_tier is used exactly as before."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "ok"},
    ])
    retrieval = StubRetrieval({"f.md": "x"})
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_haiku = registry.resolve_tier("haiku")

    res = _orch(provider, retrieval).run("q")  # no model_hint
    assert res.kind == "answer"
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier, before the real answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_haiku]


def test_model_hint_on_deep_step():
    """model_hint flows into the model argument passed to deep runner."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "deep", "goal": "do work", "deep_brief": "work", "rationale": "needs it"},
    ])
    retrieval = StubRetrieval({})
    deep = StubDeepRunner(met=True, output="done")
    res = _orch(provider, retrieval, deep_runner=deep).run("do work", model_hint="haiku")
    assert res.kind == "deep"
    # The model passed to deep runner must be the registry's "haiku" resolution.
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_haiku = registry.resolve_tier("haiku")
    assert deep.calls[0]["model"] == expected_haiku


def test_model_hint_unknown_value_degrades_gracefully():
    """An unrecognised model_hint (neither tier name nor known model id) falls back to the
    registry default (sonnet) and the run still completes normally."""
    provider = _ModelCapturingProvider(decisions=[
        {"action": "answer", "rationale": "ok"},
    ])
    retrieval = StubRetrieval({"f.md": "x"})
    # "xyzzy" is not a known tier — resolve_tier falls back to "sonnet".
    res = _orch(provider, retrieval).run("q", model_hint="xyzzy")
    assert res.kind == "answer"
    from quest_ai_runner.core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    expected_fallback = registry.resolve_tier("xyzzy")  # == sonnet fallback
    # Fix 13's always-on cheap goal-condition derivation call (STAGE 1) makes its OWN answer() call
    # first, on the cheap "fast" tier (unaffected by the answer-step hint/fallback), before the
    # real answer call.
    expected_fast = registry.resolve_tier("fast")
    assert provider.answer_models == [expected_fast, expected_fallback]


# --- attachments threading (multimodal) -------------------------------------

class _AttachmentCapturingProvider(StubProvider):
    """Captures the planner prompts and the final answer message content (incl. list/native
    blocks). Its answer() handles BOTH a string content and a content-block LIST."""

    def __init__(self, decisions, models=None):
        super().__init__(decisions, models=models)
        self.answer_contents = []

    def answer(self, messages, *, model, system=None):
        self.answer_calls += 1
        self.last_answer_messages = messages
        self.answer_contents = [m.get("content") for m in messages]
        return "ANSWER"


def _png_attachment():
    return {"filename": "chart.png", "mime_type": "image/png",
            "data": b"\x89PNG\r\n\x1a\n fake", "kind": "image"}


def test_attachments_native_image_reaches_answer_and_planner_context():
    # Default models list includes claude-* (vision); the answering model is vision-capable, so the
    # image goes NATIVE on the answer and a text note goes to the planner context.
    provider = _AttachmentCapturingProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "look at the chart"},
    ])
    res = _orch(provider, StubRetrieval()).run("what does this show?",
                                               attachments=[_png_attachment()])
    assert res.kind == "answer"
    # The final answer message carried a content LIST with a native image block.
    final = provider.answer_contents[-1]
    assert isinstance(final, list)
    assert any(isinstance(b, dict) and b.get("type") == "image" for b in final)
    # The planner saw the attachment inventory in the CONTEXT block.
    assert "chart.png" in provider.plan_prompts[0]
    assert "ATTACHMENTS" in provider.plan_prompts[0]


def test_attachments_described_when_provider_cannot_send_native():
    # The answering provider declares it cannot transmit native image blocks (like the keyless
    # CLI), so even with a vision-capable model id the image is DESCRIBED via the vision_provider
    # and NO native block reaches the answer.
    class _Describer(StubProvider):
        def __init__(self):
            super().__init__(decisions=[])
            self.described = 0
        def answer(self, messages, *, model, system=None):
            self.described += 1
            return "TRANSCRIBED: a line chart trending up."

    describer = _Describer()
    provider = _AttachmentCapturingProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    provider.supports_native_images = False             # text-only transport, like the CLI
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), vision_provider=describer)
    res = orch.run("what does this show?", attachments=[_png_attachment()])
    assert res.kind == "answer"
    assert describer.described == 1                      # the image was transcribed
    # No native image block on the answer (provider can't send them).
    final = provider.answer_contents[-1]
    assert isinstance(final, str)                        # plain-string answer message
    assert "TRANSCRIBED" in provider.plan_prompts[0]     # description grounded the planner


def test_no_attachments_is_unchanged_behavior():
    provider = _AttachmentCapturingProvider(decisions=[
        {"action": "answer", "rationale": "ok"},
    ])
    res = _orch(provider, StubRetrieval()).run("hi")
    assert res.kind == "answer"
    # Final answer message is a plain string (no content-block list when there are no attachments).
    assert isinstance(provider.answer_contents[-1], str)
    assert "ATTACHMENTS" not in provider.plan_prompts[0]


# --- narration / instant-ack beat contract --------------------------------------------------

class _CapturingSink:
    """A minimal ProgressSink that records every event the orchestrator emits."""

    def __init__(self):
        self.events = []

    def update(self, event, mode):
        self.events.append(event)


def test_instant_ack_emits_narration_flagged_partial():
    """The instant-ack / narration beat must be emitted as EVENT_PARTIAL tagged
    data={'narration': True}. The terminal UIs (interactive.py, textual_ui.py) gate on that flag to
    render it as a dim "thinking out loud" line; if the flag is missing or renamed they misroute the
    beat into the streamed-answer buffer and the immediate response never shows. Regression for the
    narration/ack key mismatch.
    """
    from quest_ai_runner.core.orchestrator import EVENT_PARTIAL

    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval({"README.md": "x"}), provider=provider,
                        registry=ModelRegistry(provider),
                        config=OrchestratorConfig(instant_ack=True))
    sink = _CapturingSink()
    orch.run("hello there", sink=sink)

    narration = [e for e in sink.events
                 if e.type == EVENT_PARTIAL and isinstance(e.data, dict) and e.data.get("narration")]
    assert narration, "instant_ack should emit at least one narration-flagged EVENT_PARTIAL beat"
    # The exact predicate the terminal UIs use to recognize a narration beat (vs an answer token).
    for e in narration:
        assert e.data.get("narration") or e.data.get("ack")


def test_narrator_first_beat_emits_from_background_without_flush():
    """The instant response must go out the moment the model returns, NOT wait for the main pipeline
    (context search, guidance) to reach flush_first(). The first beat emits itself from its
    background thread; flush_first() is only an ordering join. Proven by asserting the beat is
    already in the sink once begin()'s background future completes, before flush_first() is called.
    """
    from quest_ai_runner.core.orchestrator import Narrator, _Emitter, Mode, EVENT_PARTIAL

    events = []

    class _Sink:
        def update(self, ev, mode):
            events.append(ev)

    class _Provider:
        def answer(self, messages, *, model, system=None):
            return "Looking into the multilingual plan now."

    emit = _Emitter(_Sink(), Mode.LIVE, lambda _m: None)
    narrator = Narrator(provider=_Provider(), model="m", emit=emit, enabled=True)
    narrator.begin("what are the gaps in multilingual support?")
    # Wait for the BACKGROUND beat to finish WITHOUT calling flush_first().
    narrator._first_future.result(timeout=5.0)

    beats = [e for e in events if e.type == EVENT_PARTIAL and e.data.get("narration")]
    assert beats, "the first beat must emit itself from the background thread, before flush_first()"
    # flush_first() must NOT double-emit (it only joins now).
    narrator.flush_first()
    beats_after = [e for e in events if e.type == EVENT_PARTIAL and e.data.get("narration")]
    assert len(beats_after) == len(beats), "flush_first must not re-emit the already-emitted beat"


def test_narration_rationale_instructions_demand_grounding_discipline():
    """The relayed planner-rationale beats must not assert ungrounded conclusions before the search
    is complete. Step 0 (nothing read yet) must name what it's about to check, not what it expects;
    re-plan beats must speak only to what GATHERED shows and voice unconfirmed hunches as hunches.
    """
    from quest_ai_runner.core.orchestrator import (
        _RATIONALE_INSTRUCTION_NARRATE,
        _RATIONALE_INSTRUCTION_NARRATE_REPLAN,
    )

    # Step 0: must forbid stating expected findings/conclusions before any read.
    assert "never what you expect to find or conclude" in _RATIONALE_INSTRUCTION_NARRATE

    # Re-plan: must require honesty about how much was actually seen, and hedge unconfirmed hunches.
    replan = _RATIONALE_INSTRUCTION_NARRATE_REPLAN
    assert "what you ACTUALLY found in GATHERED" in replan
    assert "I haven't found" in replan                       # absence stated as not-found-yet
    assert "not a fact" in replan                            # hunches voiced as hunches
    assert "settled" in replan                               # no conclusion stated as settled early


# ---------------------------------------------------------------------------
# Cooperative mid-run cancellation (``cancel_check``).
# ---------------------------------------------------------------------------

def test_cancel_check_true_from_the_start_skips_planning_entirely():
    """A cancel_check that already reports True before the first step must stop the run BEFORE any
    planner call, returning kind="cancelled" (not a normal answer/deep/confirm outcome)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "should never run"}])
    res = _orch(provider, StubRetrieval()).run("hello", cancel_check=lambda: True)
    assert res.kind == "cancelled"
    assert provider.plan_calls == 0
    # No answer/deep/confirm fields are populated for a cancelled result.
    assert res.text is None
    assert res.deep_results == []
    assert res.decision_id is None


def test_cancel_check_stops_after_the_first_step_before_the_second_plan_call():
    """cancel_check flips True only AFTER the first step: the loop must run the first plan/read
    step to completion, then stop at the NEXT step boundary rather than starting a second one."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "reading"},
        {"action": "answer", "rationale": "should never run"},
    ])
    retrieval = StubRetrieval({"README.md": "fact: yes"})
    calls = {"n": 0}

    def cancel_after_first_step() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    res = _orch(provider, retrieval).run("q", cancel_check=cancel_after_first_step)
    assert res.kind == "cancelled"
    assert provider.plan_calls == 1              # the read step ran; the answer step never fired
    assert retrieval.read_calls == ["README.md"]  # the first step's own work still completed


def test_cancel_check_false_never_affects_a_normal_run():
    """A cancel_check that always returns False (or a caller that passes None) must leave the run
    byte-for-byte unaffected -- the existing plan -> read -> answer behavior is unchanged."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "need the doc"},
        {"action": "answer", "rationale": "have it"},
    ])
    retrieval = StubRetrieval({"README.md": "GROUNDING fact: pricing is $9/mo."})
    res = _orch(provider, retrieval).run("What's the price?", cancel_check=lambda: False)
    assert res.kind == "answer"
    assert retrieval.read_calls == ["README.md"]
    # Same call counts test_plan_read_then_answer asserts for the identical scripted plan (a
    # cancel_check that is always False changes nothing about the loop's own behavior).
    assert provider.plan_calls == 4
    assert provider.answer_calls == 2


def test_cancel_check_true_before_deep_execution_returns_cancelled_without_running_the_worker():
    """A plan that would escalate to 'deep' must not spawn the worker at all when the run was
    already cancelled by the time the deep step is about to start."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the one-pager", "deep_brief": "do it", "rationale": "work"},
    ])
    runner = StubDeepRunner(met=True, output="one-pager written")
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "write the one-pager", cancel_check=lambda: True)
    assert res.kind == "cancelled"
    assert runner.calls == []                    # the deep worker never ran
    assert res.deep_results == []


def test_cancel_check_during_deep_retry_stops_before_the_next_attempt():
    """A deep goal that fails verification normally retries (see
    ``test_goal_loop_iterates_until_verified_met``, whose SAME scripted plan drives 2 worker runs).
    Here cancel_check flips True as soon as the first attempt has run, so the retry loop must stop
    at the next-attempt boundary instead of spawning a second worker run."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": False, "reason": "the fix was incomplete", "next_action": "also update the helper"},
        {"met": True, "reason": "done"},   # would only be consumed by a second attempt's verify call
    ])
    runner = StubDeepRunner(met=True, output="made an edit")

    def cancel_after_first_attempt() -> bool:
        return len(runner.calls) >= 1

    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deep_goal_max_iterations=3)).run(
        "fix the thing", cancel_check=cancel_after_first_attempt)
    assert res.kind == "cancelled"
    assert len(runner.calls) == 1                # only the first attempt ran; retry never fired


# --- WS1 reliability floor: loud, configurable timeouts -----------------------------------

class _SlowRetrieval:
    """A RetrievalAdapter where reading ``slow.md`` blocks past the configured per-op timeout;
    every other path (grep/query, other paths) resolves immediately."""

    def __init__(self, delay: float, files: Dict[str, str]):
        import time as _t
        self._time = _t
        self.delay = delay
        self.files = files

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None, max_bytes=None):
        from quest_ai_runner.core.adapters import Observation
        if rel_path == "slow.md":
            self._time.sleep(self.delay)
        if rel_path not in self.files:
            return Observation(kind="error", rel_path=rel_path, error="not found")
        return Observation(kind="read", rel_path=rel_path, locator="head", text=self.files[rel_path])

    def grep(self, pattern, *, scope=None, max_hits=None):
        from quest_ai_runner.core.adapters import Observation
        return Observation(kind="grep", pattern=pattern, hits=[])

    def query(self, spec):
        from quest_ai_runner.core.adapters import Observation
        return Observation(kind="error", error="query unsupported in stub")


def test_read_op_timeout_names_the_stalled_operation(monkeypatch):
    # WS1 fix: a single slow adapter member must not wedge the whole read step, and its timeout
    # must be a NAMED error (not an empty result indistinguishable from "nothing found"). The
    # OTHER concurrent read in the same step must still succeed normally.
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "0.05")
    retrieval = _SlowRetrieval(delay=0.3, files={"slow.md": "x", "fast.md": "fast content"})
    provider = StubProvider(decisions=[])
    orch = _orch(provider, retrieval)

    results = orch._do_reads([{"rel_path": "slow.md"}, {"rel_path": "fast.md"}])

    assert len(results) == 2
    slow_result, fast_result = results[0], results[1]
    assert slow_result["kind"] == "error"
    assert "slow.md" in slow_result["error"]
    assert "timed out" in slow_result["error"]
    assert fast_result["kind"] == "read"
    assert fast_result["text"] == "fast content"


def test_read_op_timeout_default_is_generous(monkeypatch):
    # No env override -> the documented default (60s), not some accidental tiny value.
    from quest_ai_runner.core.orchestrator import read_op_timeout_seconds
    monkeypatch.delenv("QAR_READ_OP_TIMEOUT_SECONDS", raising=False)
    assert read_op_timeout_seconds() == 60.0
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "12.5")
    assert read_op_timeout_seconds() == 12.5
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "not-a-number")
    assert read_op_timeout_seconds() == 60.0  # bad value falls back, never raises


def test_context_assembly_timeout_is_loud_and_recoverable(monkeypatch, caplog):
    # WS1 fix: a slow context assembler must not (a) silently drop turn-start context at debug
    # level only, or (b) hang the turn waiting for it. The turn proceeds without the fresh
    # context, a WARNING names what happened, and EVENT_CONTEXT carries a structured
    # "assembly_timed_out" marker a consumer can observe.
    import logging
    import time as time_module

    from quest_ai_runner.core.adapters import AssembledContext
    from quest_ai_runner.core.orchestrator import EVENT_CONTEXT

    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "0.05")

    class SlowAssembler:
        def assemble(self, task_text, *, meta=None):
            time_module.sleep(0.3)
            return AssembledContext(context_view="TOO_LATE_CONTEXT")

        def record(self, task_text, outcome):
            pass

    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=SlowAssembler())
    sink = _CapturingSink()
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.orchestrator"):
        res = orch.run("hello there", sink=sink)

    assert res.kind == "answer"
    # The slow assembler's context never reached the planner prompt: dropped, not awaited.
    assert not any("TOO_LATE_CONTEXT" in p for p in provider.plan_prompts)
    assert any("timed out" in r.message.lower() and "dropped" in r.message.lower()
              for r in caplog.records), "the timeout must be a loud WARNING, not a debug breadcrumb"
    ctx_events = [e for e in sink.events if e.type == EVENT_CONTEXT]
    assert ctx_events, "EVENT_CONTEXT must still fire on a timeout so a consumer can observe it"
    assert any(e.data.get("assembly_timed_out") for e in ctx_events)


def test_context_assembly_timeout_default_is_five_seconds(monkeypatch):
    from quest_ai_runner.core.orchestrator import context_assembly_timeout_seconds
    monkeypatch.delenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", raising=False)
    assert context_assembly_timeout_seconds() == 5.0
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "2.5")
    assert context_assembly_timeout_seconds() == 2.5
    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "garbage")
    assert context_assembly_timeout_seconds() == 5.0  # bad value falls back, never raises


def test_context_assembly_partial_result_is_used_and_loud(monkeypatch, caplog):
    # A PARTIAL assembly result (the assembler hit its soft deadline and returned only the
    # arm(s) that completed) must be USED — it beats the drop-everything path — while staying
    # as loud as the timeout: a WARNING naming the degradation plus a structured
    # "assembly_partial" marker on EVENT_CONTEXT.
    import logging

    from quest_ai_runner.core.adapters import AssembledContext
    from quest_ai_runner.core.orchestrator import EVENT_CONTEXT

    class PartialAssembler:
        def assemble(self, task_text, *, meta=None):
            return AssembledContext(context_view="PARTIAL_CONTEXT", partial=True)

        def record(self, task_text, outcome):
            pass

    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=PartialAssembler())
    sink = _CapturingSink()
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.orchestrator"):
        res = orch.run("hello there", sink=sink)

    assert res.kind == "answer"
    # The partial context reached the planner prompt — used, not dropped.
    assert any("PARTIAL_CONTEXT" in p for p in provider.plan_prompts)
    assert any("partial" in r.message.lower() for r in caplog.records), (
        "using a partial result must be a loud WARNING naming the degradation")
    ctx_events = [e for e in sink.events if e.type == EVENT_CONTEXT]
    assert ctx_events
    assert any(e.data.get("assembly_partial") for e in ctx_events)
    # Partial is NOT a timeout: the full-drop marker must stay false.
    assert not any(e.data.get("assembly_timed_out") for e in ctx_events)


def test_context_assembly_meta_carries_soft_deadline(monkeypatch):
    # The turn-start prefetch threads a soft deadline (a time.monotonic() timestamp slightly
    # under the hard collect timeout) to the assembler via meta["assembly_deadline"], so a
    # deadline-aware assembler can return best-effort partials in time.
    import time as time_module

    from quest_ai_runner.core.adapters import AssembledContext

    seen_meta: dict = {}

    class RecordingAssembler:
        def assemble(self, task_text, *, meta=None):
            seen_meta.update(meta or {})
            return AssembledContext(context_view="CTX")

        def record(self, task_text, outcome):
            pass

    monkeypatch.setenv("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "5")
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=RecordingAssembler())
    before = time_module.monotonic()
    orch.run("hello there", sink=_CapturingSink())

    deadline = seen_meta.get("assembly_deadline")
    assert isinstance(deadline, float), f"expected a monotonic deadline, got: {deadline!r}"
    # Soft deadline sits UNDER the 5s hard budget, measured from roughly the submit time.
    assert before + 1.0 < deadline <= time_module.monotonic() + 5.0


def test_partial_turn_start_not_cached_midloop_read_recovers_full():
    # A PARTIAL turn-start result is used for THIS turn's prompt but must never be registered
    # as the query's completed cached result: a later mid-loop {"cards": <same query>} read
    # falls through to a fresh, deadline-free assemble and recovers the FULL fuse (the
    # late-recovery contract a registered partial would displace).
    from quest_ai_runner.core.adapters import AssembledContext

    class PartialThenFullAssembler:
        def __init__(self):
            self.calls = []  # (task_text, had_deadline)

        def assemble(self, task_text, *, meta=None):
            had_deadline = (meta or {}).get("assembly_deadline") is not None
            self.calls.append((task_text, had_deadline))
            if had_deadline:
                return AssembledContext(context_view="PARTIAL_CONTEXT", partial=True)
            return AssembledContext(context_view="FULL_CONTEXT")

        def record(self, task_text, outcome):
            pass

    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"cards": "hello there"}], "rationale": "recheck topic"},
        {"action": "answer", "rationale": "done", "model_tier": "haiku"},
    ])
    assembler = PartialThenFullAssembler()
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=assembler)
    res = orch.run("hello there", sink=_CapturingSink())

    assert res.kind == "answer"
    # The partial WAS used for the current turn's prompt (beats dropping fresh context).
    assert any("PARTIAL_CONTEXT" in p for p in provider.plan_prompts)
    # Same query at turn start (deadline-bounded) and mid-loop: the mid-loop read must NOT be
    # served the partial from the cache -- it re-assembles fresh (no deadline) and gets FULL.
    assert assembler.calls == [("hello there", True), ("hello there", False)]
    cards_obs = [o for o in res.gathered
                 if isinstance(o, dict) and str(o.get("locator", "")).startswith("cards(")]
    assert cards_obs, f"no cards observation in gathered: {res.gathered}"
    assert "FULL_CONTEXT" in cards_obs[0]["text"]
    assert "PARTIAL_CONTEXT" not in cards_obs[0]["text"]


def test_empty_partial_assembly_is_not_reported_as_used(caplog):
    # An EMPTY partial (partial=True with no context_view) means nothing was actually used:
    # the "PARTIAL context was used" WARNING and the assembly_partial marker must NOT fire.
    # The degradation still stays loud under its own truthful name.
    import logging

    from quest_ai_runner.core.adapters import AssembledContext
    from quest_ai_runner.core.orchestrator import EVENT_CONTEXT

    class EmptyPartialAssembler:
        def assemble(self, task_text, *, meta=None):
            return AssembledContext(partial=True)

        def record(self, task_text, outcome):
            pass

    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider),
                        context_assembler=EmptyPartialAssembler())
    sink = _CapturingSink()
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.orchestrator"):
        res = orch.run("hello there", sink=sink)

    assert res.kind == "answer"
    assert not any("partial context was used" in r.message.lower() for r in caplog.records), (
        "an empty partial used nothing; claiming partial context was used would be untrue")
    assert any("empty" in r.message.lower() and "partial" in r.message.lower()
               for r in caplog.records), "the empty-partial degradation must still be loud"
    ctx_events = [e for e in sink.events if e.type == EVENT_CONTEXT]
    assert ctx_events
    assert not any(e.data.get("assembly_partial") for e in ctx_events)
    assert not any(e.data.get("assembly_timed_out") for e in ctx_events)


# ---------------------------------------------------------------------------
# deferred_deep wording: the words must match the configured mechanism (inline vs queued).
# ---------------------------------------------------------------------------

def test_deferred_deep_wording_matches_configuration():
    """The deferred_deep wording must describe the ACTUAL behavior of each configuration.

    INLINE (default, deferred_deep_queued=False): the work runs synchronously right after the
    answer, in the same turn; the doctrine and schema must say that and never describe a queue.
    QUEUED (deferred_deep_queued=True): the consumer wired a runner that enqueues the work as a
    background task; the doctrine and schema must say the work is queued in the background and
    the user is told when it finishes, and never claim it runs in the same turn."""
    from quest_ai_runner.core import orchestrator as orch_mod

    # Inline semantics: same-turn, no queue.
    inline = orch_mod.DEFERRED_DEEP_INLINE_SEMANTICS
    assert "SAME" in inline and "turn" in inline
    assert "queue" not in inline.lower()
    inline_desc = orch_mod.DECIDE_TOOL["input_schema"]["properties"]["deferred_deep"]["description"]
    assert "immediately after the answer" in inline_desc
    assert "same turn" in inline_desc
    assert "queue" not in inline_desc.lower()

    # Queued semantics: background queue + report-back, no same-turn claim.
    queued = orch_mod.DEFERRED_DEEP_QUEUED_SEMANTICS
    assert "background" in queued.lower()
    assert "when the background work finishes" in queued
    assert "SAME turn" not in queued
    queued_tool = orch_mod.decide_tool_for(False, True)
    queued_desc = queued_tool["input_schema"]["properties"]["deferred_deep"]["description"]
    assert "background task queue" in queued_desc
    assert "same turn" not in queued_desc.lower()
    # The variant is a copy: the shared base schema keeps its inline description.
    assert (orch_mod.DECIDE_TOOL["input_schema"]["properties"]["deferred_deep"]["description"]
            == inline_desc)
    # decide_tool_for with the flag off returns the base schemas untouched.
    assert orch_mod.decide_tool_for(False, False) is orch_mod.DECIDE_TOOL
    assert orch_mod.decide_tool_for(True, False) is orch_mod.DECIDE_TOOL_WITH_MODE_SIGNAL
    # Queued + mode signals keeps the mode_signal field alongside the queued description.
    both = orch_mod.decide_tool_for(True, True)
    assert "mode_signal" in both["input_schema"]["properties"]
    assert ("background task queue"
            in both["input_schema"]["properties"]["deferred_deep"]["description"])


def test_planner_prompt_carries_the_configured_deferred_semantics():
    """The rendered planner prompt carries the semantics sentence matching the configuration:
    inline (default) says same-turn; queued says background + report-back. Both truthful."""
    from quest_ai_runner.core import orchestrator as orch_mod

    # Default (inline) configuration.
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider, StubRetrieval()).run("hello")
    assert orch_mod.DEFERRED_DEEP_INLINE_SEMANTICS in provider.plan_prompts[0]
    assert orch_mod.DEFERRED_DEEP_QUEUED_SEMANTICS not in provider.plan_prompts[0]
    inline_dd = provider.plan_tool_schemas[0]["input_schema"]["properties"]["deferred_deep"]
    assert "same turn" in inline_dd["description"]

    # Queued configuration.
    provider2 = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    _orch(provider2, StubRetrieval(),
          config=OrchestratorConfig(deferred_deep_queued=True)).run("hello")
    assert orch_mod.DEFERRED_DEEP_QUEUED_SEMANTICS in provider2.plan_prompts[0]
    assert orch_mod.DEFERRED_DEEP_INLINE_SEMANTICS not in provider2.plan_prompts[0]
    queued_dd = provider2.plan_tool_schemas[0]["input_schema"]["properties"]["deferred_deep"]
    assert "background task queue" in queued_dd["description"]


# ---------------------------------------------------------------------------
# Deferred hand-off (queued deployments): the dormant DeepResult.deferred contract, active.
# ---------------------------------------------------------------------------

def test_deferred_handoff_short_circuits_and_reports_queued():
    """A queued deployment's deferred_deep: the runner confirms the enqueue and returns
    DeepResult(met=True, deferred=True, output=<sentinel>). The goal loop must trust met and
    stop (ONE runner call: no re-verify of the sentinel, no relaunch), the reply must be
    re-synthesized through the queued hand-off prompt (grounded in the sentinel, reporting the
    work as queued rather than done), and the turn's exit_reason must be "deferred" (the answer
    goal-verification loop is skipped: the goal is intentionally not met yet)."""
    sentinel = "Queued as task #77: research the vendor options."
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Research vendor options",
                                      "brief": "compare vendors",
                                      "rationale": "long-running"}}],
        answer_text="I'll look into the vendors.",
    )
    runner = StubDeepRunner(met=True, output=sentinel, deferred=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deferred_deep_queued=True)).run(
        "research vendor options overnight")
    assert res.kind == "answer"
    assert res.exit_reason == "deferred"
    assert len(runner.calls) == 1, "a deferred hand-off must never relaunch or re-enqueue"
    # The final reply came from the queued synthesis prompt, grounded in the hand-off sentinel.
    joined = "\n".join(
        (m["content"] if isinstance(m["content"], str) else str(m["content"]))
        for m in provider.last_answer_messages)
    assert "CONFIRMED HAND-OFF RECORD" in joined
    assert sentinel in joined
    assert "ACTUAL RESULT OF THE WORK YOU JUST DID" not in joined, (
        "a queue receipt must never be presented as finished work")


def test_deferred_handoff_pins_the_registered_deferred_runner():
    """With deferred_deep_queued on and a runner registered under the reserved 'deferred' key,
    the deferred_deep hand-off must go to THAT runner even when the classifier would route the
    goal elsewhere (deferred work must reach the queue, never an inline runner)."""
    sentinel = "Queued as task #5."
    queue_runner = StubDeepRunner(met=True, output=sentinel, deferred=True)
    inline_runner = StubDeepRunner(met=True, output="did it inline")
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
    )
    res = _orch(provider, StubRetrieval(),
                deep_runners={"deferred": queue_runner, "inline": inline_runner},
                deep_runner_classifier=lambda msg, goal, brief: "inline",
                config=OrchestratorConfig(deferred_deep_queued=True)).run("do the thing later")
    assert res.exit_reason == "deferred"
    assert len(queue_runner.calls) == 1
    assert inline_runner.calls == [], "classifier must not re-route deferred work inline"


def test_deferred_handoff_failure_reply_does_not_claim_queued():
    """HONEST-ENQUEUE: in a queued deployment, when the hand-off is NOT confirmed (the enqueue
    failed), the reply must be regenerated with a steer saying the work was NOT queued; it must
    never pass the failure through the queued hand-off synthesis."""
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
        answer_text="I'll queue that up.",
    )
    runner = StubDeepRunner(met=False, output="", error="enqueue failed: api down")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deferred_deep_queued=True,
                                          answer_goal_max_iterations=1,
                                          verify_claims=False)).run("do the thing later")
    assert res.kind == "answer"
    assert res.exit_reason != "deferred"
    # The regeneration steer told the model the hand-off failed and forbade claiming queued.
    all_answer_prompts = "\n".join(
        (m["content"] if isinstance(m["content"], str) else str(m["content"]))
        for m in provider.last_answer_messages)
    assert "FAILED" in all_answer_prompts
    assert "has NOT been queued" in all_answer_prompts
    assert "CONFIRMED HAND-OFF RECORD" not in all_answer_prompts


def test_inline_default_never_uses_queued_synthesis():
    """Regression: with deferred_deep_queued off (the default), a deferred_deep still runs
    inline in the same turn and folds back through the after-deep synthesis, exactly as before;
    the queued hand-off prompt must not appear."""
    deliverable = "INLINE_DELIVERABLE: the analysis."
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then run",
                    "deferred_deep": {"goal": "Analyze it"}}],
    )
    runner = StubDeepRunner(met=True, output=deliverable)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("analyze it")
    assert res.kind == "answer"
    assert res.exit_reason != "deferred"
    assert runner.calls, "inline deferred_deep must still execute in the same turn"
    joined = "\n".join(
        (m["content"] if isinstance(m["content"], str) else str(m["content"]))
        for m in provider.last_answer_messages)
    assert "CONFIRMED HAND-OFF RECORD" not in joined


# ---------------------------------------------------------------------------
# Deferred hand-off: the honesty edges. A queued deployment may be MISCONFIGURED (no runner under
# the reserved key, a runner that lies about its enqueue, a classifier reaching for the reserved
# key) and the reply must still describe what really happened this turn.
# ---------------------------------------------------------------------------

def _all_answer_prompts(provider) -> str:
    """Every answer() prompt the provider saw this run, joined (not just the last one)."""
    return "\n".join(
        (m["content"] if isinstance(m["content"], str) else str(m["content"]))
        for msgs in provider.all_answer_messages for m in msgs)


def test_queued_mode_without_deferred_runner_reports_the_inline_work_it_really_did():
    """FINDING 1 (the important one): a queued consumer whose ``deep_runners`` map LACKS (or
    typos) the reserved 'deferred' key. The hand-off pin then resolves to nothing, the normal
    wiring executes the deferred work FOR REAL and inline, and it produces output.

    The turn must report that real output (fold-back through the after-deep synthesis). It must
    NOT take the honest-enqueue branch, which would tell the user nothing was queued or done while
    the work was in fact done, and would throw the deliverable away."""
    deliverable = "INLINE_DELIVERABLE: vendor A is cheapest."
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Research vendor options"}}],
        answer_text="Here is what I know so far.",
    )
    # The consumer MEANT to register the queue runner but typoed the reserved key.
    inline_runner = StubDeepRunner(met=True, output=deliverable)
    res = _orch(provider, StubRetrieval(),
                deep_runners={"defered": inline_runner, "inline": inline_runner},
                deep_runner_classifier=lambda msg, goal, brief: "inline",
                config=OrchestratorConfig(deferred_deep_queued=True,
                                          answer_goal_max_iterations=1,
                                          verify_claims=False)).run("research vendor options")
    assert res.kind == "answer"
    assert inline_runner.calls, "the work really ran (no queue runner was reachable)"
    assert res.exit_reason != "deferred", "nothing was queued: this was not a hand-off"
    prompts = _all_answer_prompts(provider)
    # The real output was folded back through the after-deep synthesis...
    assert deliverable in prompts, "the inline deliverable must reach the final reply"
    assert "ACTUAL RESULT OF THE WORK YOU JUST DID" in prompts
    # ...and the turn never claimed the work was not queued/started/done.
    assert "has NOT been queued" not in prompts, (
        "the work RAN inline; claiming nothing happened would be a false not-queued claim")
    assert "CONFIRMED HAND-OFF RECORD" not in prompts


def test_queued_mode_does_not_trust_a_deferred_result_that_carries_an_error():
    """FINDING 2: a consumer runner that returns met=True, deferred=True on a FAILED enqueue must
    not be believed. Only a deferred result that is met, error-free AND carries a receipt is a
    confirmed hand-off; anything else takes the honest not-queued path."""
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
        answer_text="I'll queue that up.",
    )
    runner = StubDeepRunner(met=True, deferred=True, output="Queued as task #9.",
                            error="enqueue rejected: 500 from the task API")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deferred_deep_queued=True,
                                          answer_goal_max_iterations=1,
                                          verify_claims=False)).run("do the thing later")
    assert res.exit_reason != "deferred", "a failed enqueue is not a hand-off, whatever met says"
    prompts = _all_answer_prompts(provider)
    assert "has NOT been queued" in prompts
    assert "CONFIRMED HAND-OFF RECORD" not in prompts, (
        "an errored hand-off must never be synthesized as queued")


def test_queued_mode_does_not_trust_an_empty_deferred_receipt():
    """FINDING 2 (second half): deferred + met but NO receipt at all. There is nothing to report
    as queued, so the turn takes the honest not-queued path rather than inventing a hand-off."""
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
        answer_text="I'll queue that up.",
    )
    runner = StubDeepRunner(met=True, deferred=True, output="")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deferred_deep_queued=True,
                                          answer_goal_max_iterations=1,
                                          verify_claims=False)).run("do the thing later")
    assert res.exit_reason != "deferred"
    assert "has NOT been queued" in _all_answer_prompts(provider)


def test_classifier_may_not_select_the_reserved_deferred_runner_key():
    """FINDING 3: the reserved 'deferred' key is reachable ONLY through the deferred hand-off's
    explicit runner_override. A classifier returning it for an ORDINARY deep turn would hand the
    goal loop a queue receipt to verify, and the caller would report a receipt as finished work.
    Such a key is rejected: the default runner takes the turn."""
    queue_runner = StubDeepRunner(met=True, output="Queued as task #12.", deferred=True)
    default_runner = StubDeepRunner(met=True, output="the real work product")
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the report", "deep_brief": "do it"},
        {"met": True, "reason": "report written"},
    ])
    res = _orch(provider, StubRetrieval(), deep_runner=default_runner,
                deep_runners={"deferred": queue_runner},
                deep_runner_classifier=lambda msg, goal, brief: "deferred",
                config=OrchestratorConfig(deferred_deep_queued=True)).run("write the report")
    assert res.kind == "deep"
    assert queue_runner.calls == [], "the reserved queue runner is not classifier-selectable"
    assert default_runner.calls, "an ordinary deep turn falls back to the default runner"
    assert res.deep_results[0].deferred is False


def test_honest_enqueue_has_a_deterministic_floor_when_the_rewrite_call_fails():
    """FINDING 4: the honest-enqueue rewrite is itself an LLM call. If it throws, the reply would
    fall back to the pre-deep draft, which under queued doctrine may already claim the work is
    queued. The correction must not depend on a model call: a plain not-queued sentence is
    appended deterministically."""
    from quest_ai_runner.core.orchestrator import NOT_QUEUED_NOTE

    class _RegenFailsProvider(StubProvider):
        """Answers normally until the honest-enqueue regeneration, then goes down."""

        def answer(self, messages, *, model, system=None):
            joined = "\n".join(
                (m["content"] if isinstance(m["content"], str) else str(m["content"]))
                for m in messages)
            if "hand this work to the background queue FAILED" in joined:
                raise RuntimeError("model outage during the honesty rewrite")
            return super().answer(messages, model=model, system=system)

    provider = _RegenFailsProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
        # The pre-deep draft was written under queued doctrine: it already claims the queue.
        answer_text="I have queued this to run in the background.",
    )
    runner = StubDeepRunner(met=False, output="", error="enqueue failed: api down")
    res = _orch(provider, StubRetrieval(), deep_runner=runner,
                config=OrchestratorConfig(deferred_deep_queued=True,
                                          answer_goal_max_iterations=1,
                                          verify_claims=False)).run("do the thing later")
    assert res.kind == "answer"
    assert res.exit_reason != "deferred"
    assert NOT_QUEUED_NOTE in (res.text or ""), (
        "with the rewrite down, the reply must still say plainly that nothing was queued")
    assert "has NOT been queued" in NOT_QUEUED_NOTE


def test_queue_only_wiring_can_still_hand_off():
    """FINDING 6: a consumer that registers ONLY the queue runner (no default deep_runner, no
    classifier) has no INLINE execution capability, but the deferred hand-off pins its runner
    explicitly, so the queue must still be reachable."""
    sentinel = "Queued as task #33."
    queue_runner = StubDeepRunner(met=True, output=sentinel, deferred=True)
    provider = StubProvider(
        decisions=[{"action": "answer", "rationale": "answer then queue",
                    "deferred_deep": {"goal": "Do the thing"}}],
    )
    res = _orch(provider, StubRetrieval(),
                deep_runners={"deferred": queue_runner},
                config=OrchestratorConfig(deferred_deep_queued=True)).run("do the thing later")
    assert res.exit_reason == "deferred"
    assert len(queue_runner.calls) == 1


def test_render_planner_prompt_fills_every_slot_by_default():
    """FINDING 5b: PLANNER_PROMPT gained a mandatory {deferred_deep_semantics} slot, so a raw
    .format() by an external consumer now raises KeyError. render_planner_prompt is the
    non-breaking path: pass what you have, defaults fill the rest."""
    from quest_ai_runner.core.orchestrator import (
        DEFERRED_DEEP_INLINE_SEMANTICS,
        DEFERRED_DEEP_QUEUED_SEMANTICS,
        render_planner_prompt,
    )

    rendered = render_planner_prompt(user_message="what is the price?")
    assert "what is the price?" in rendered
    assert "{deferred_deep_semantics}" not in rendered
    assert "{mode_signal_block}" not in rendered
    # The default deployment is INLINE, so the default wording must be the inline one.
    assert DEFERRED_DEEP_INLINE_SEMANTICS in rendered
    queued = render_planner_prompt(user_message="x",
                                   deferred_deep_semantics=DEFERRED_DEEP_QUEUED_SEMANTICS)
    assert DEFERRED_DEEP_QUEUED_SEMANTICS in queued


# ---------------------------------------------------------------------------
# Cross-turn narration memory (``prior_narration`` / ``OrchestratorResult.narration_said``).
# ---------------------------------------------------------------------------

def test_narrator_prior_narration_suppresses_repeated_ack():
    """A fresh Narrator is built every turn, so without help the ack has no memory of what it said
    in EARLIER turns of the same conversation, only of _said (this turn). Seeding prior_narration
    with a line already spoken must let the repeat-detector catch and suppress an ack that repeats
    it, not just repeats within a single turn."""
    from quest_ai_runner.core.orchestrator import EVENT_PARTIAL, Mode, Narrator, _Emitter

    events = []

    class _Sink:
        def update(self, ev, mode):
            events.append(ev)

    class _Provider:
        def answer(self, messages, *, model, system=None):
            return "Let me look into that for you."

    emit = _Emitter(_Sink(), Mode.LIVE, lambda _m: None)
    narrator = Narrator(
        provider=_Provider(), model="m", emit=emit, enabled=True,
        prior_narration=["Let me look into that for you."],
    )
    narrator.begin("what's on my plate today?")
    narrator._first_future.result(timeout=5.0)

    beats = [e for e in events if e.type == EVENT_PARTIAL and e.data.get("narration")]
    assert beats == [], "an ack repeating a prior-turn line must be suppressed, not spoken again"


def test_narrator_prior_narration_shown_in_ack_prompt():
    """The ack prompt must show prior-turn narration as its own explicit 'said in earlier turns, do
    not repeat this shape' block, distinct from this-turn's _said list, so the model itself (not
    just the word-overlap backstop) has a real chance to vary or go quiet."""
    from quest_ai_runner.core.orchestrator import Mode, Narrator, _Emitter

    seen_prompts = []

    class _Sink:
        def update(self, ev, mode):
            pass

    class _Provider:
        def answer(self, messages, *, model, system=None):
            seen_prompts.append(messages[-1]["content"])
            return "Checking your marathon plan specifically now."

    emit = _Emitter(_Sink(), Mode.LIVE, lambda _m: None)
    narrator = Narrator(
        provider=_Provider(), model="m", emit=emit, enabled=True,
        prior_narration=["Let me look into that for you."],
    )
    narrator.begin("what's my next step?")
    narrator._first_future.result(timeout=5.0)

    assert seen_prompts, "the ack call must have run"
    prompt = seen_prompts[0]
    assert "EARLIER turns" in prompt
    assert "Let me look into that for you." in prompt


def test_orchestrator_result_narration_said_round_trips_into_next_turn():
    """End to end: OrchestratorResult.narration_said carries forward what the narrator actually
    said, and feeding it back in as the next run's prior_narration suppresses a would-be-identical
    repeat ack -- the exact pattern a voice consumer uses so the ack stops reopening every turn with
    its own recent generic line."""
    from quest_ai_runner.core.orchestrator import EVENT_PARTIAL

    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"},
    ])
    orch = _orch(provider, StubRetrieval({"README.md": "x"}),
                 config=OrchestratorConfig(instant_ack=True))

    sink1 = _CapturingSink()
    res1 = orch.run("hello there", sink=sink1)
    assert res1.narration_said, "the ack should have produced at least one narration line"

    # Reset the scripted decisions for the second turn (StubProvider consumes its queue).
    provider._decisions = [{"action": "answer", "rationale": "chit-chat", "model_tier": "haiku"}]
    sink2 = _CapturingSink()
    res2 = orch.run("hello again", sink=sink2, prior_narration=res1.narration_said)

    narration2 = [e for e in sink2.events
                 if e.type == EVENT_PARTIAL and isinstance(e.data, dict) and e.data.get("narration")]
    # StubProvider.answer() ignores prompt content and always returns the same fixed ack text
    # regardless of turn, so without prior_narration wired through this would repeat every turn;
    # with it threaded through run() -> Narrator, the second turn's identical ack is suppressed.
    assert narration2 == []
    assert res2.kind == "answer"
