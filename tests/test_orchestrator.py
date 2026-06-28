"""Core loop: plan -> read -> re-plan -> answer, plus deep / confirm / cap fallback."""
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
    # plan_calls = 2 loop steps + 1 post-answer verification call (answer_goal_max_iterations)
    assert provider.plan_calls == 3
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
    assert provider.answer_calls == 2  # regenerated once after verify said not-met


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
    assert provider.answer_calls == 1  # verified met on the first answer, no wasted regeneration


def test_answer_not_verified_without_a_quality_bar():
    # No GuidanceProvider wired => no quality bar => no answer verification (single generation).
    provider = StubProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}],
        answer_text="an answer",
    )
    res = _orch(provider, StubRetrieval(),
                config=OrchestratorConfig(answer_goal_max_iterations=2)).run("explain X")
    assert res.kind == "answer"
    assert provider.answer_calls == 1


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
    # Use plan_prompts[-2]: the last PLANNER prompt; plan_prompts[-1] is the post-answer
    # verification call (answer_goal_max_iterations), which uses the VERIFY_GOAL_PROMPT, not
    # the planner prompt — so "EARLIER READS" would not appear there.
    last_prompt = provider.plan_prompts[-2]
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
    # plan_prompts[0..1] are the 2 loop steps; plan_prompts[2] is the post-answer verification
    # call (answer_goal_max_iterations) using VERIFY_GOAL_PROMPT — not checked here.
    assert len(provider.plan_prompts) == 3
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
    # plan_prompts[0..1] are the 2 loop steps; plan_prompts[2] is the post-answer verification
    # call (answer_goal_max_iterations) using VERIFY_GOAL_PROMPT — not checked here.
    assert len(provider.plan_prompts) == 3
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
    assert provider.answer_models == [expected_opus]


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
    assert all(m == expected_planner for m in provider.plan_models)


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
    assert provider.answer_models == [expected_haiku]


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
    assert provider.answer_models == [expected_fallback]


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
